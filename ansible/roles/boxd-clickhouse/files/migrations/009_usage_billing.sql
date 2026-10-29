-- Network deltas on `usage`, and time per VM state in the hourly rollup.

ALTER TABLE usage
    ADD COLUMN IF NOT EXISTS net_rx_delta_bytes UInt64 DEFAULT 0 COMMENT 'ACTUAL, DELTA: bytes received by the VM (ingress) since the previous sample. Aggregate with SUM. 0 in cold states.' AFTER cpu_delta_us,
    ADD COLUMN IF NOT EXISTS net_tx_delta_bytes UInt64 DEFAULT 0 COMMENT 'ACTUAL, DELTA: bytes sent by the VM (egress) since the previous sample. Aggregate with SUM. 0 in cold states.' AFTER net_rx_delta_bytes;

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
    sumIf(usage.reserved_vcpu, state = 'running')              AS reserved_vcpu_sec_run,
    sumIf(usage.reserved_ram_bytes, state IN ('running', 'suspended')) AS reserved_ram_bytesec_rs,
    sum(usage.reserved_disk_bytes)                             AS reserved_disk_bytesec,
    sum(net_rx_delta_bytes)                                    AS net_rx_bytes,
    sum(net_tx_delta_bytes)                                    AS net_tx_bytes
FROM usage FINAL
WHERE timestamp >= toStartOfHour(now()) - INTERVAL 3 HOUR
GROUP BY hour, user_id, vm_id, vm_name;
