-- Per-second resource usage per VM: actual use and reserved allocation, in every VM state.

CREATE TABLE IF NOT EXISTS usage (
    timestamp DateTime64(3) COMMENT 'Sample time (ms). ~1 row per VM per second.',
    user_id String COMMENT 'VM owner — the billing key.',
    vm_id String COMMENT 'VM UUID (stable identity).',
    vm_name String DEFAULT '' COMMENT 'VM name (mutable; can be reused after destroy).',
    node_id String DEFAULT '' COMMENT 'Worker that emitted the row.',
    state String DEFAULT '' COMMENT 'VM status at sample time (running/starting/suspended/hibernated/stopped/failed/pending/migrating). destroyed is never emitted.',
    cpu_delta_us UInt64 DEFAULT 0 COMMENT 'ACTUAL, DELTA: CPU-time consumed since the previous sample, microseconds. Aggregate with SUM (SUM/1e6 = CPU-seconds) — NOT a level, do not avg. 0 in cold states (no live vmm). Quantized to 10ms (kernel jiffies).',
    memory_bytes UInt64 DEFAULT 0 COMMENT 'ACTUAL, GAUGE: resident RAM (RSS) at sample time, bytes. Aggregate with avg/max; SUM over 1s rows = byte-seconds. 0 in cold states (RAM freed); real while running or warm-suspended.',
    disk_bytes UInt64 DEFAULT 0 COMMENT 'ACTUAL, GAUGE: disk occupied, bytes = st_blocks of the CoW overlay alone (which reflink-contains the base image content once; rows before the 2026-08 worker fix also added the base st_blocks on top, double-counting it). avg/max; SUM = byte-seconds. Present in every non-destroyed state (overlay persists).',
    reserved_vcpu UInt32 DEFAULT 0 COMMENT 'RESERVED, level: provisioned vCPU count. SUM over 1s rows = vCPU-seconds. To compare with cpu_delta_us: reserved_vcpu * 1e6 = µs of CPU available that second.',
    reserved_ram_bytes UInt64 DEFAULT 0 COMMENT 'RESERVED, level: provisioned RAM, bytes. Directly comparable to memory_bytes. SUM over 1s rows = byte-seconds.',
    reserved_disk_bytes UInt64 DEFAULT 0 COMMENT 'RESERVED, level: provisioned disk, bytes. Directly comparable to disk_bytes. SUM over 1s rows = byte-seconds.',
    INDEX idx_user user_id TYPE bloom_filter GRANULARITY 4,
    INDEX idx_vm vm_id TYPE bloom_filter GRANULARITY 4
) ENGINE = ReplacingMergeTree()
ORDER BY (user_id, vm_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 120 DAY;
