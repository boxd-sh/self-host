-- 011 dropped `usage_hourly.active_used_seconds` ("nothing reads them"),
-- which stopped being true once boxd-admin's onboarding KPI (crates/
-- boxd-admin/src/analytics.rs) needed the identical "sustained in-VM
-- usage" signal and, lacking the rollup column, computed it LIVE off the
-- raw per-second `usage` table instead. That works on small data but
-- doesn't scale: `usage`'s ORDER BY is (user_id, vm_id, timestamp), so a
-- per-user cohort scan across a wide time window reads every second of
-- every one of that user's VMs — one real dev-cluster user with 30 VMs
-- alone pushed a single KPI query to 3.1M rows read, blowing the `reader`
-- account's 1M max_rows_to_read (Code: 158, TOO_MANY_ROWS) and 500ing
-- `/api/v1/analytics/kpis`. Restoring the hourly rollup cuts that to
-- ~1 row per VM per hour — roughly a 3600x reduction.
--
-- Forward-only (house convention, same as 011's own rename): no backfill.
-- Existing hourly rows read this new column as the DEFAULT 0 until enough
-- real time passes post-migration for the MV to populate it going
-- forward — the sustained-usage KPI signal will undercount for existing
-- windows for a while. Acceptable for a v1 internal ops-tool KPI; not
-- used for billing. Refreshable MV can't be ALTERed → drop, alter,
-- recreate; re-runs idempotently on every ansible pass, like every prior
-- migration touching this view.
DROP VIEW IF EXISTS usage_hourly_mv;

ALTER TABLE usage_hourly
    ADD COLUMN IF NOT EXISTS active_used_seconds UInt32 DEFAULT 0 COMMENT 'BILLABLE-style "actual usage time": seconds with real CPU work OR real network traffic (cpu_delta_us>50000, >5% of a core, OR net_rx_delta_bytes+net_tx_delta_bytes>1000, clears ARP/DNS/DHCP/teardown chatter). Read by boxd-admin''s onboarding KPI (real_activity_sustained_usage) — see crates/boxd-admin/src/analytics.rs.';

-- Recreate the MV (definition only; data untouched). Column order mirrors
-- the post-ALTER table exactly (position-aligned): active_used_seconds
-- lands at the very end, after 011's used_ram_bytesec_rs/net columns.
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
    -- reserved_* aliases above shadow the raw columns (unqualified =>
    -- nested aggregation error). memory_bytes is NOT shadowed (its
    -- aliases are mem_avg_bytes / mem_peak_bytes) so it needs no qualifier.
    sumIf(usage.reserved_vcpu, state = 'running')              AS reserved_vcpu_sec_run,
    sumIf(memory_bytes, state IN ('running', 'suspended'))     AS used_ram_bytesec_rs,
    sum(usage.reserved_disk_bytes)                             AS reserved_disk_bytesec,
    sum(net_rx_delta_bytes)                                    AS net_rx_bytes,
    sum(net_tx_delta_bytes)                                    AS net_tx_bytes,
    toUInt32(countIf(cpu_delta_us > 50000 OR net_rx_delta_bytes + net_tx_delta_bytes > 1000)) AS active_used_seconds
FROM usage FINAL
WHERE timestamp >= toStartOfHour(now()) - INTERVAL 3 HOUR
GROUP BY hour, user_id, vm_id, vm_name;
