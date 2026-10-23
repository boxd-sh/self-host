-- Two corrections to the usage_hourly rollup, in one MV rebuild:
--
-- 1. Drop the activity-classification counters — nothing reads them, and the
--    raw per-second signal stays in `usage` (120d) if ever needed again:
--      active_cpu_seconds  (009)  active_net_seconds  (010)  active_used_seconds (010)
--
-- 2. Bill RAM on ACTUAL usage, not reserved. The billable RAM integral summed
--    the fixed 8 GiB shape; it now sums resident RSS (memory_bytes). The state
--    window is unchanged and correct: host RAM is committed only while running
--    or suspended; hibernated/stopped free it (snapshot to disk) and the worker
--    writes memory_bytes=0 there anyway. Column renamed to reflect the flip:
--      reserved_ram_bytesec_rs -> used_ram_bytesec_rs
--
-- Kept: the reserved RAM snapshot (reserved_ram_bytes) and the vCPU/disk
-- reserved integrals. Forward-only (house convention): no backfill, historical
-- rows reset the renamed column to 0. Refreshable MV can't be ALTERed → drop,
-- alter, recreate; re-runs idempotently on every ansible pass.
DROP VIEW IF EXISTS usage_hourly_mv;

ALTER TABLE usage_hourly
    DROP COLUMN IF EXISTS active_cpu_seconds,
    DROP COLUMN IF EXISTS active_net_seconds,
    DROP COLUMN IF EXISTS active_used_seconds,
    DROP COLUMN IF EXISTS reserved_ram_bytesec_rs,
    ADD COLUMN IF NOT EXISTS used_ram_bytesec_rs UInt64 DEFAULT 0 COMMENT 'BILLABLE integral: ACTUAL RAM byte-seconds — SUM of resident RSS (memory_bytes) while running OR suspended, the states where host RAM is committed. Hibernated/stopped free RAM (snapshot to disk) and contribute 0.' AFTER reserved_vcpu_sec_run;

-- Recreate the MV (definition only; data untouched). Column order mirrors the
-- post-ALTER table exactly (position-aligned): no active_* counters, and
-- used_ram_bytesec_rs keeps the old integral's slot, sourced from memory_bytes.
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
    SUM(cpu_delta_us)                                          AS cpu_us_total,
    toUInt64(avg(memory_bytes))                                AS mem_avg_bytes,
    max(memory_bytes)                                          AS mem_peak_bytes,
    max(disk_bytes)                                            AS disk_peak_bytes,
    max(reserved_vcpu)                                         AS reserved_vcpu,
    max(reserved_ram_bytes)                                    AS reserved_ram_bytes,
    max(reserved_disk_bytes)                                   AS reserved_disk_bytes,
    now()                                                      AS version,
    -- reserved_vcpu / reserved_disk are usage.-qualified: the max(...) AS
    -- reserved_* aliases above shadow the raw columns (unqualified => nested
    -- aggregation error). memory_bytes is NOT shadowed (its aliases are
    -- mem_avg_bytes / mem_peak_bytes) so it needs no qualifier.
    sumIf(usage.reserved_vcpu, state = 'running')              AS reserved_vcpu_sec_run,
    sumIf(memory_bytes, state IN ('running', 'suspended'))     AS used_ram_bytesec_rs,
    sum(usage.reserved_disk_bytes)                             AS reserved_disk_bytesec,
    sum(net_rx_delta_bytes)                                    AS net_rx_bytes,
    sum(net_tx_delta_bytes)                                    AS net_tx_bytes
FROM usage FINAL
WHERE timestamp >= toStartOfHour(now()) - INTERVAL 3 HOUR
GROUP BY hour, user_id, vm_id, vm_name;
