-- 15-minute rollup of `usage` (`usage_rollup`), created alongside the hourly one.

CREATE TABLE IF NOT EXISTS usage_rollup (
    bucket DateTime COMMENT 'Start of the 15-minute bucket this row summarizes. The billing period: one ledger charge per org per settled bucket.',
    user_id String COMMENT 'VM owner — billing key.',
    org_id String DEFAULT '' COMMENT 'Billing org stamped at write time (empty = personal; resolves to the owner''s default org, same as the sweeper).',
    org_link String DEFAULT '' COMMENT 'shared | private | '''' — how the VM relates to the org.',
    vm_id String COMMENT 'VM UUID.',
    vm_name String DEFAULT '' COMMENT 'VM name at the time.',
    sample_seconds UInt32 DEFAULT 0 COMMENT 'Samples in the bucket (~ seconds the VM was tracked).',
    running_seconds UInt32 DEFAULT 0 COMMENT 'Seconds in running state.',
    suspended_seconds UInt32 DEFAULT 0 COMMENT 'Seconds in suspended (warm standby) — bills RAM+disk.',
    hibernated_seconds UInt32 DEFAULT 0 COMMENT 'Seconds in hibernated (cold) — bills disk only.',
    stopped_seconds UInt32 DEFAULT 0 COMMENT 'Seconds in stopped — bills disk only.',
    cpu_us_total UInt64 DEFAULT 0 COMMENT 'Total CPU consumed, microseconds (SUM of cpu_delta_us).',
    mem_avg_bytes UInt64 DEFAULT 0 COMMENT 'Average resident RAM over the bucket, bytes.',
    mem_peak_bytes UInt64 DEFAULT 0 COMMENT 'Peak resident RAM in the bucket, bytes.',
    disk_peak_bytes UInt64 DEFAULT 0 COMMENT 'Peak disk occupied in the bucket, bytes.',
    reserved_vcpu UInt32 DEFAULT 0 COMMENT 'Provisioned vCPU (snapshot).',
    reserved_ram_bytes UInt64 DEFAULT 0 COMMENT 'Provisioned RAM, bytes (snapshot).',
    reserved_disk_bytes UInt64 DEFAULT 0 COMMENT 'Provisioned disk, bytes (snapshot). Comparable to disk_peak_bytes.',
    version DateTime DEFAULT now() COMMENT 'Refresh time; ReplacingMergeTree keeps the latest row per (user_id, vm_id, bucket). Reads MUST use FINAL.',
    reserved_vcpu_sec_run UInt64 DEFAULT 0 COMMENT 'BILLABLE integral: vCPU-seconds while running (SUM of reserved_vcpu over running samples).',
    used_ram_bytesec_rs UInt64 DEFAULT 0 COMMENT 'BILLABLE integral: ACTUAL RAM byte-seconds — SUM of resident RSS (memory_bytes) while running OR suspended. Hibernated/stopped free RAM and contribute 0.',
    used_disk_bytesec UInt64 DEFAULT 0 COMMENT 'BILLABLE integral: ACTUAL disk byte-seconds — SUM of disk_bytes (host st_blocks of the CoW overlay, which reflink-contains the base image content once) across ALL sampled time.',
    net_rx_bytes UInt64 DEFAULT 0 COMMENT 'Total bytes received by the VM (ingress) this bucket.',
    net_tx_bytes UInt64 DEFAULT 0 COMMENT 'Total bytes sent by the VM (egress) this bucket.',
    active_used_seconds UInt32 DEFAULT 0 COMMENT 'Seconds with real CPU work OR real network traffic (cpu_delta_us>50000 OR rx+tx>1000).'
) ENGINE = ReplacingMergeTree(version)
ORDER BY (user_id, vm_id, bucket)
TTL bucket + INTERVAL 2 YEAR;

DROP VIEW IF EXISTS usage_rollup_mv;

CREATE MATERIALIZED VIEW IF NOT EXISTS usage_rollup_mv
    REFRESH EVERY 5 MINUTE APPEND TO usage_rollup AS
SELECT
    toStartOfFifteenMinutes(timestamp)                        AS bucket,
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
    sumIf(usage.reserved_vcpu, state = 'running')              AS reserved_vcpu_sec_run,
    sumIf(memory_bytes, state IN ('running', 'suspended'))     AS used_ram_bytesec_rs,
    sum(disk_bytes)                                            AS used_disk_bytesec,
    sum(net_rx_delta_bytes)                                    AS net_rx_bytes,
    sum(net_tx_delta_bytes)                                    AS net_tx_bytes,
    toUInt32(countIf(cpu_delta_us > 50000 OR net_rx_delta_bytes + net_tx_delta_bytes > 1000)) AS active_used_seconds
FROM usage FINAL
WHERE timestamp >= toStartOfFifteenMinutes(now()) - INTERVAL 30 MINUTE
GROUP BY bucket, user_id, vm_id, vm_name;
