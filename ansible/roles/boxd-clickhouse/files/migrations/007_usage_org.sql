-- Org attribution on `usage` and its hourly rollup, stamped when each row is written.

ALTER TABLE usage
    ADD COLUMN IF NOT EXISTS org_id   LowCardinality(String) DEFAULT '' AFTER user_id,
    ADD COLUMN IF NOT EXISTS org_link LowCardinality(String) DEFAULT '' AFTER org_id;

ALTER TABLE usage_hourly
    ADD COLUMN IF NOT EXISTS org_id   LowCardinality(String) DEFAULT '' AFTER user_id,
    ADD COLUMN IF NOT EXISTS org_link LowCardinality(String) DEFAULT '' AFTER org_id;

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
    toUInt32(countIf(cpu_delta_us > 50000 OR memory_bytes > 536870912)) AS active_seconds,
    SUM(cpu_delta_us)                                          AS cpu_us_total,
    toUInt64(avg(memory_bytes))                                AS mem_avg_bytes,
    max(memory_bytes)                                          AS mem_peak_bytes,
    max(disk_bytes)                                            AS disk_peak_bytes,
    max(reserved_vcpu)                                         AS reserved_vcpu,
    max(reserved_ram_bytes)                                    AS reserved_ram_bytes,
    max(reserved_disk_bytes)                                   AS reserved_disk_bytes,
    now()                                                      AS version
FROM usage FINAL
WHERE timestamp >= toStartOfHour(now()) - INTERVAL 3 HOUR
GROUP BY hour, user_id, vm_id, vm_name;
