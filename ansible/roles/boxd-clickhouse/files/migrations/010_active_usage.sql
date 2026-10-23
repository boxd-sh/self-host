-- Activity classification, take 2: count network-active seconds, count the
-- billable "actual usage time" union, and retire the legacy blended rule.
--
--   active_cpu_seconds  (009)  — the machine COMPUTED     (cpu_delta_us > 50000, >5% of a core)
--   active_net_seconds  (new)  — someone/something USED IT (rx+tx > 1000 B/s at the tap)
--   active_used_seconds (new)  — the union, per the billing model: "actual
--                                usage time = seconds with real CPU work or
--                                real network traffic". Needs its own counted
--                                column — a union is not derivable from the
--                                two parts (a second can be both).
--
-- The legacy `active_seconds` (cpu>50000 OR memory_bytes>512MB) is DROPPED:
-- resident RAM is warmth, not usage — any long-running service with a fat RSS
-- read as "100% actively used" while nobody touched it. Memory stays available
-- as the mem_avg/mem_peak gauges for efficiency/right-sizing views. No code
-- consumed the column (checked console/, crates/, Grafana dashboards).
--
-- THE 1000 B/s THRESHOLD (calibrated on staging, 2026-07-03; revisit against
-- the prod census before the prod rollout — raw retains 120d, so history can
-- be re-emitted under a corrected constant):
--   * idle floor: 0 bytes/s — per-network bridges mean no L2 neighbors, and
--     every CPU-idle running second sampled carried literally zero bytes.
--   * protocol chatter (ARP refresh, DNS, DHCP renew, TCP teardown ticks):
--     ~40-600 B in isolated seconds.
--   * smallest real interaction observed (one proxied HTTP GET): >4 KB in its
--     second; a minimal tiny-response request computes to ~700-1000 B.
--   1000 clears all chatter with margin and catches any request that moves
--   meaningful data. Applied to rx+tx so outbound-only workloads count.
DROP VIEW IF EXISTS usage_hourly_mv;

ALTER TABLE usage_hourly
    DROP COLUMN IF EXISTS active_seconds;

ALTER TABLE usage_hourly
    ADD COLUMN IF NOT EXISTS active_net_seconds  UInt32 DEFAULT 0 COMMENT 'Seconds with real network traffic: net_rx+net_tx > 1000 B/s at the tap. The "someone is using this VM" signal; threshold clears ARP/DNS/DHCP/teardown chatter (<=600B events).',
    ADD COLUMN IF NOT EXISTS active_used_seconds UInt32 DEFAULT 0 COMMENT 'BILLABLE "actual usage time": seconds with real CPU work OR real traffic (cpu_delta_us>50000 OR net>1000). Union column — not derivable from active_cpu_seconds + active_net_seconds. >= each part.';

-- Recreate the refreshable MV (definition only; data untouched; re-runs
-- idempotently on every ansible pass). Column order mirrors the post-ALTER
-- table exactly (position-aligned): active_seconds is gone from the middle,
-- the two new counters land at the END after the 009 columns.
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
    -- raw columns (unqualified => nested aggregation error).
    sumIf(usage.reserved_vcpu, state = 'running')              AS reserved_vcpu_sec_run,
    sumIf(usage.reserved_ram_bytes, state IN ('running', 'suspended')) AS reserved_ram_bytesec_rs,
    sum(usage.reserved_disk_bytes)                             AS reserved_disk_bytesec,
    sum(net_rx_delta_bytes)                                    AS net_rx_bytes,
    sum(net_tx_delta_bytes)                                    AS net_tx_bytes,
    toUInt32(countIf(net_rx_delta_bytes + net_tx_delta_bytes > 1000)) AS active_net_seconds,
    toUInt32(countIf(cpu_delta_us > 50000 OR net_rx_delta_bytes + net_tx_delta_bytes > 1000)) AS active_used_seconds
FROM usage FINAL
WHERE timestamp >= toStartOfHour(now()) - INTERVAL 3 HOUR
GROUP BY hour, user_id, vm_id, vm_name;
