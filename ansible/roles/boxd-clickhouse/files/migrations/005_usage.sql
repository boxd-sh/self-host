-- Durable per-VM resource usage: actual utilization (twin of `metrics`) plus
-- reserved/provisioned allocation, covering every VM state. Unlike `metrics`
-- (1h TTL, live console charts only) this table retains raw per-second rows for
-- 120 days — long enough to bill/reconcile/dispute within the window. Per-user
-- totals are computed at read time (GROUP BY user_id). One row per VM per
-- sample second.
--
-- NOTE: 120d covers the raw granularity only. Long-term usage history beyond
-- the window needs a rolled-up aggregate table (deferred) fed from these rows
-- before they age out; the TTL deletes raw rows, it does not down-sample them.
--
-- ENGINE is ReplacingMergeTree keyed on the full ORDER BY (user_id, vm_id,
-- timestamp): the worker emits exactly one row per VM per sample second, and
-- the analytics client re-queues a batch on send failure — so a partial-failure
-- retry (ClickHouse committed the INSERT but the ack was lost) re-sends
-- byte-identical rows. Replacing collapses those same-key duplicates to one,
-- preventing billing over-counts. Dedup is eventual (at merge time), so exact
-- billing reads MUST use `SELECT ... FINAL` or pre-aggregate by the sort key,
-- e.g. `SELECT user_id, SUM(cpu_delta_us) FROM usage FINAL WHERE user_id = ? GROUP BY user_id`.
--
-- AGGREGATION RULE — the actual columns are NOT all the same kind, so they do
-- not aggregate the same way (column COMMENTs below restate this, visible via
-- DESCRIBE / system.columns):
--   * cpu_delta_us is a DELTA (CPU consumed since the previous sample) → use SUM.
--   * memory_bytes / disk_bytes are GAUGES (instantaneous levels)  → use avg/max
--     (or SUM over the 1-row-per-second cadence for byte-seconds).
--   * reserved_* are LEVELS (constant per VM shape) → snapshot, or SUM over the
--     1s cadence for reserved unit-seconds (e.g. vCPU-seconds).
-- Mixing SUM/avg correctly is the one easy mistake here; pick per the kind.
CREATE TABLE IF NOT EXISTS usage (
    timestamp DateTime64(3) COMMENT 'Sample time (ms). ~1 row per VM per second.',
    user_id String COMMENT 'VM owner — the billing key.',
    vm_id String COMMENT 'VM UUID (stable identity).',
    vm_name String DEFAULT '' COMMENT 'VM name (mutable; can be reused after destroy).',
    node_id String DEFAULT '' COMMENT 'Worker that emitted the row.',
    state String DEFAULT '' COMMENT 'VM status at sample time (running/starting/suspended/hibernated/stopped/failed/pending/migrating). destroyed is never emitted.',
    -- ACTUAL usage — NOTE the mixed aggregation semantics (see AGGREGATION RULE):
    cpu_delta_us UInt64 DEFAULT 0 COMMENT 'ACTUAL, DELTA: CPU-time consumed since the previous sample, microseconds. Aggregate with SUM (SUM/1e6 = CPU-seconds) — NOT a level, do not avg. 0 in cold states (no live vmm). Quantized to 10ms (kernel jiffies).',
    memory_bytes UInt64 DEFAULT 0 COMMENT 'ACTUAL, GAUGE: resident RAM (RSS) at sample time, bytes. Aggregate with avg/max; SUM over 1s rows = byte-seconds. 0 in cold states (RAM freed); real while running or warm-suspended.',
    disk_bytes UInt64 DEFAULT 0 COMMENT 'ACTUAL, GAUGE: disk occupied, bytes = st_blocks of the CoW overlay alone (which reflink-contains the base image content once; rows before the 2026-08 worker fix also added the base st_blocks on top, double-counting it). avg/max; SUM = byte-seconds. Present in every non-destroyed state (overlay persists).',
    -- RESERVED / provisioned allocation — a LEVEL (constant per VM shape),
    -- repeated each row by design so reads stay single-table. The per-state
    -- billing matrix is applied at read time, not here.
    reserved_vcpu UInt32 DEFAULT 0 COMMENT 'RESERVED, level: provisioned vCPU count. SUM over 1s rows = vCPU-seconds. To compare with cpu_delta_us: reserved_vcpu * 1e6 = µs of CPU available that second.',
    reserved_ram_bytes UInt64 DEFAULT 0 COMMENT 'RESERVED, level: provisioned RAM, bytes. Directly comparable to memory_bytes. SUM over 1s rows = byte-seconds.',
    reserved_disk_bytes UInt64 DEFAULT 0 COMMENT 'RESERVED, level: provisioned disk, bytes. Directly comparable to disk_bytes. SUM over 1s rows = byte-seconds.',
    INDEX idx_user user_id TYPE bloom_filter GRANULARITY 4,
    INDEX idx_vm vm_id TYPE bloom_filter GRANULARITY 4
) ENGINE = ReplacingMergeTree()
ORDER BY (user_id, vm_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 120 DAY;
