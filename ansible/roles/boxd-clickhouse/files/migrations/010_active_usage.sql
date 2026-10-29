-- Network-active and active-use seconds in the hourly rollup; drops `active_seconds`.

DROP VIEW IF EXISTS usage_hourly_mv;

ALTER TABLE usage_hourly
    DROP COLUMN IF EXISTS active_seconds;

ALTER TABLE usage_hourly
    ADD COLUMN IF NOT EXISTS active_net_seconds  UInt32 DEFAULT 0 COMMENT 'Seconds with real network traffic: net_rx+net_tx > 1000 B/s at the tap. The "someone is using this VM" signal; threshold clears ARP/DNS/DHCP/teardown chatter (<=600B events).',
    ADD COLUMN IF NOT EXISTS active_used_seconds UInt32 DEFAULT 0 COMMENT 'BILLABLE "actual usage time": seconds with real CPU work OR real traffic (cpu_delta_us>50000 OR net>1000). Union column — not derivable from active_cpu_seconds + active_net_seconds. >= each part.';

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
