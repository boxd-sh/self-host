-- Hourly rollup of `usage`: one row per VM per clock-hour, every metric
-- pre-aggregated the right way (cpu_delta_us summed; memory/disk gauges
-- avg/max; reserved snapshot) plus derived running/active seconds. This is the
-- long-term, query-cheap layer that billing + dashboards read; the raw `usage`
-- table (120d) stays for per-second drill-down.
--
-- Engine: ReplacingMergeTree(version) keyed on (user_id, vm_id, hour). Each
-- refresh RE-COMPUTES the recent hours in full from `usage FINAL` and APPENDs;
-- Replacing keeps the newest version per bucket, so the still-open hour is
-- corrected on the next refresh and closed hours become immutable. Retains 2
-- years (tunable) — independent of raw's 120d, so history outlives the raw
-- rows. Reads MUST use FINAL (or GROUP BY the sort key).
CREATE TABLE IF NOT EXISTS usage_hourly (
    hour DateTime COMMENT 'Start of the clock-hour this row summarizes.',
    user_id String COMMENT 'VM owner — billing key.',
    vm_id String COMMENT 'VM UUID.',
    vm_name String DEFAULT '' COMMENT 'VM name at the time.',
    sample_seconds UInt32 DEFAULT 0 COMMENT 'Samples in the hour (≈ seconds the VM was tracked).',
    running_seconds UInt32 DEFAULT 0 COMMENT 'Seconds in running state — billable running time.',
    active_seconds UInt32 DEFAULT 0 COMMENT 'Seconds with real activity: cpu_delta_us>50000 (>5% of a core) OR memory_bytes>512MB.',
    cpu_us_total UInt64 DEFAULT 0 COMMENT 'Total CPU consumed, microseconds (SUM of cpu_delta_us). /3.6e9 = CPU-hours.',
    mem_avg_bytes UInt64 DEFAULT 0 COMMENT 'Average resident RAM over the hour, bytes.',
    mem_peak_bytes UInt64 DEFAULT 0 COMMENT 'Peak resident RAM in the hour, bytes.',
    disk_peak_bytes UInt64 DEFAULT 0 COMMENT 'Peak disk used in the hour, bytes.',
    reserved_vcpu UInt32 DEFAULT 0 COMMENT 'Provisioned vCPU (snapshot).',
    reserved_ram_bytes UInt64 DEFAULT 0 COMMENT 'Provisioned RAM, bytes (snapshot).',
    reserved_disk_bytes UInt64 DEFAULT 0 COMMENT 'Provisioned disk, bytes (snapshot).',
    version DateTime DEFAULT now() COMMENT 'Refresh time; ReplacingMergeTree keeps the latest row per (user_id, vm_id, hour).'
) ENGINE = ReplacingMergeTree(version)
ORDER BY (user_id, vm_id, hour)
TTL hour + INTERVAL 2 YEAR;

-- Refreshable view: every hour, recompute the last 3 hours from raw and append.
-- (3h window covers the still-open hour + late raw after a worker/CH blip.)
-- The body is just the read-time rollup SELECT, wrapped to run on a schedule.
CREATE MATERIALIZED VIEW IF NOT EXISTS usage_hourly_mv
    REFRESH EVERY 1 HOUR APPEND TO usage_hourly AS
SELECT
    toStartOfHour(timestamp)                                  AS hour,
    user_id,
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
