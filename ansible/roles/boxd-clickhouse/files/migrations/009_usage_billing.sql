-- Billing-grade usage: network deltas on the raw facts + a state-aware hourly
-- rollup. The billing model charges reserved capacity BY STATE (running →
-- CPU+RAM+disk; suspended → RAM+disk; hibernated/stopped → disk only) and
-- counts "actual usage time" as seconds with real CPU work OR real network
-- traffic — inbound traffic is boxd's own definition of "someone is using this
-- VM" (it drives auto-suspend/wake), so a VM serving requests at ~0 CPU must
-- still read as active. Three gaps closed here:
--   1. usage_hourly only recorded running_seconds → RAM during standby
--      (suspended) and the other non-running states weren't billable.
--   2. reserved_* were hour-level max() snapshots → a VM resized mid-hour was
--      mis-billed. The reserved_*_sec integrals below are exact under mid-hour
--      resizes AND state changes (state-conditional SUM over 1-per-second rows).
--   3. No network signal anywhere → "actual usage" was blind to traffic.
--
-- Forward-only, like 007: no backfill; historical rows keep the column
-- defaults (0). The usage_hourly sort key (user_id, vm_id, hour) is UNCHANGED
-- so the MV's 3-hour re-emit keeps replacing prior rows via
-- ReplacingMergeTree(version).

-- Raw per-second facts: per-sample network deltas, stamped by the worker's
-- analytics loop from the VM's tap counters. Same DELTA semantics as
-- cpu_delta_us (see the AGGREGATION RULE in 005_usage.sql): bytes since the
-- previous sample, 0 in cold states → aggregate with SUM. Direction is
-- VM-perspective: rx = into the VM (ingress), tx = out of the VM. `usage` is
-- written via JSONEachRow (matched by name), so column placement is cosmetic.
ALTER TABLE usage
    ADD COLUMN IF NOT EXISTS net_rx_delta_bytes UInt64 DEFAULT 0 COMMENT 'ACTUAL, DELTA: bytes received by the VM (ingress) since the previous sample. Aggregate with SUM. 0 in cold states.' AFTER cpu_delta_us,
    ADD COLUMN IF NOT EXISTS net_tx_delta_bytes UInt64 DEFAULT 0 COMMENT 'ACTUAL, DELTA: bytes sent by the VM (egress) since the previous sample. Aggregate with SUM. 0 in cold states.' AFTER net_rx_delta_bytes;

-- Hourly rollup: split non-running time by state, add a CPU-only activity
-- counter, the three billable reserved integrals, and the network sums.
-- NOTE: sample_seconds − (running+suspended+hibernated+stopped) is NOT always
-- 0 — transient states (pending/starting/migrating/failed) make up the small
-- remainder. The columns without AFTER land at the END of the table, after
-- `version` — the MV SELECT below mirrors that order exactly.
ALTER TABLE usage_hourly
    ADD COLUMN IF NOT EXISTS suspended_seconds  UInt32 DEFAULT 0 COMMENT 'Seconds in suspended (warm standby: vmm alive, vCPUs paused) — bills RAM+disk.' AFTER running_seconds,
    ADD COLUMN IF NOT EXISTS hibernated_seconds UInt32 DEFAULT 0 COMMENT 'Seconds in hibernated (cold: memory snapshot on disk) — bills disk only.' AFTER suspended_seconds,
    ADD COLUMN IF NOT EXISTS stopped_seconds    UInt32 DEFAULT 0 COMMENT 'Seconds in stopped — bills disk only.' AFTER hibernated_seconds,
    ADD COLUMN IF NOT EXISTS active_cpu_seconds UInt32 DEFAULT 0 COMMENT 'Seconds with real CPU work only (cpu_delta_us>50000, no memory clause) — always ≤ active_seconds. Add network-active seconds for full "actual usage time".' AFTER active_seconds,
    ADD COLUMN IF NOT EXISTS reserved_vcpu_sec_run   UInt64 DEFAULT 0 COMMENT 'BILLABLE integral: vCPU-seconds while running (SUM of reserved_vcpu over running samples). Exact under mid-hour resizes, unlike reserved_vcpu × running_seconds.',
    ADD COLUMN IF NOT EXISTS reserved_ram_bytesec_rs UInt64 DEFAULT 0 COMMENT 'BILLABLE integral: RAM byte-seconds while running OR suspended.',
    ADD COLUMN IF NOT EXISTS reserved_disk_bytesec   UInt64 DEFAULT 0 COMMENT 'BILLABLE integral: disk byte-seconds across ALL sampled time (disk bills in every non-destroyed state).',
    ADD COLUMN IF NOT EXISTS net_rx_bytes            UInt64 DEFAULT 0 COMMENT 'Total bytes received by the VM (ingress) this hour.',
    ADD COLUMN IF NOT EXISTS net_tx_bytes            UInt64 DEFAULT 0 COMMENT 'Total bytes sent by the VM (egress) this hour.';

-- The refreshable MV can't be ALTERed → drop + recreate (definition only;
-- usage_hourly data is untouched, and this re-runs idempotently on every
-- ansible pass). Everything from the 007 definition is kept — schedule,
-- 3-hour window, GROUP BY, org argMax — with the new aggregates added.
-- Column order mirrors the post-ALTER table order (version mid-list, the five
-- appended columns after it) so the SELECT stays position-aligned with
-- usage_hourly.
DROP VIEW IF EXISTS usage_hourly_mv;

CREATE MATERIALIZED VIEW IF NOT EXISTS usage_hourly_mv
    REFRESH EVERY 1 HOUR APPEND TO usage_hourly AS
SELECT
    toStartOfHour(timestamp)                                  AS hour,
    user_id,
    argMax(org_id, timestamp)                                 AS org_id,
    argMax(org_link, timestamp)                               AS org_link,
    vm_id,
    vm_name,
    toUInt32(count())                                          AS sample_seconds,
    toUInt32(countIf(state = 'running'))                       AS running_seconds,
    toUInt32(countIf(state = 'suspended'))                     AS suspended_seconds,
    toUInt32(countIf(state = 'hibernated'))                    AS hibernated_seconds,
    toUInt32(countIf(state = 'stopped'))                       AS stopped_seconds,
    toUInt32(countIf(cpu_delta_us > 50000 OR memory_bytes > 536870912)) AS active_seconds,
    toUInt32(countIf(cpu_delta_us > 50000))                    AS active_cpu_seconds,
    SUM(cpu_delta_us)                                          AS cpu_us_total,
    toUInt64(avg(memory_bytes))                                AS mem_avg_bytes,
    max(memory_bytes)                                          AS mem_peak_bytes,
    max(disk_bytes)                                            AS disk_peak_bytes,
    max(reserved_vcpu)                                         AS reserved_vcpu,
    max(reserved_ram_bytes)                                    AS reserved_ram_bytes,
    max(reserved_disk_bytes)                                   AS reserved_disk_bytes,
    now()                                                      AS version,
    -- usage.-qualified: the max(...) AS reserved_* aliases above shadow the
    -- raw columns, and an unqualified reference here would resolve to the
    -- alias → illegal nested aggregation.
    sumIf(usage.reserved_vcpu, state = 'running')              AS reserved_vcpu_sec_run,
    sumIf(usage.reserved_ram_bytes, state IN ('running', 'suspended')) AS reserved_ram_bytesec_rs,
    sum(usage.reserved_disk_bytes)                             AS reserved_disk_bytesec,
    sum(net_rx_delta_bytes)                                    AS net_rx_bytes,
    sum(net_tx_delta_bytes)                                    AS net_tx_bytes
FROM usage FINAL
WHERE timestamp >= toStartOfHour(now()) - INTERVAL 3 HOUR
GROUP BY hour, user_id, vm_id, vm_name;
