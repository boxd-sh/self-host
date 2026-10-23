-- Bill disk on ACTUAL usage, not reserved — the disk twin of what 011 did for
-- RAM. The billable disk integral summed the PROVISIONED size (100 GB by
-- default, `usage.reserved_disk_bytes`); it now sums the bytes actually
-- occupied on the worker's disk (`usage.disk_bytes`). A VM provisioned at
-- 100 GB that occupies 30 GB is billed for 30 GB. Column renamed to reflect
-- the flip:
--   reserved_disk_bytesec -> used_disk_bytesec
--
-- `disk_bytes` semantics: st_blocks of the CoW overlay ALONE, fixed in the
-- worker sampler in the same change as this migration. The overlay is a
-- reflink clone of the base image, so its st_blocks contains the base content
-- once — the intended bill ("a VM pays for the blocks its disk file maps").
-- The sampler previously ADDED the base st_blocks on top, double-counting it;
-- raw `usage` rows written before the fixed worker deploys therefore carry the
-- inflated definition, and rollup hours mix the two across that cutover.
-- Display-only skew: no credits org is billed from pre-cutover hours.
--
-- Kept: the reserved disk snapshot (reserved_disk_bytes) and the peak gauge
-- (disk_peak_bytes), so provisioned-vs-used stays reconcilable from the rollup
-- alone — boxd-admin's machine-usage percentage reads the raw pair.
--
-- The aggregation is deliberately UNCONDITIONAL — a bare sum(), not RAM's
-- `sumIf(..., state IN ('running','suspended'))`. RAM needed the state window
-- because hibernated/stopped VMs free it; disk is the one resource a cold VM
-- genuinely still consumes. The worker reads the overlay in EVERY non-destroyed
-- state (crates/boxd-worker/src/metrics.rs, "read it unconditionally"), and the
-- billing matrix in 009's header bills disk in every one of them. A state
-- filter here would silently make parked 100 GB VMs free.
--
-- `disk_bytes` needs NO `usage.` qualifier: its only alias in this SELECT is
-- `max(disk_bytes) AS disk_peak_bytes`, a DIFFERENT name, so it isn't shadowed
-- — same reasoning as `memory_bytes`. Only reserved_vcpu / reserved_disk_bytes,
-- whose max() aliases reuse their own names, need qualifying.
--
-- Forward-only at migration time (house convention, same as 011/012): the DROP
-- resets every historical row to DEFAULT 0 and the MV only re-emits the last 3
-- hours. backfills/2026-08-06-backfill-used-disk.sql rebuilds every hour still
-- covered by raw `usage` (120d retention) and supersedes the July used-ram
-- backfill. Run it promptly after this lands: the credit meter settles an hour
-- ~4h after it starts, so a settled-but-unmetered hour swept before the
-- backfill would be charged with disk = 0.
--
-- Refreshable MV can't be ALTERed → drop, alter, recreate. Replay semantics
-- (every *.sql re-runs on every converge, glob-ordered, per-file failures
-- tolerated by the shell loop): this file re-runs CLEAN; the earlier view
-- migrations (006-012) fail loudly-but-harmlessly on replay because ClickHouse
-- validates MV SELECT columns BY NAME against the target table, so their
-- CREATEs can never resurrect a stale view — this file runs last and always
-- re-establishes the correct one. Verified empirically on CH 25.3 (2026-08-07:
-- full-chain apply, converge-faithful replay, fixture rollup + backfill).
DROP VIEW IF EXISTS usage_hourly_mv;

ALTER TABLE usage_hourly
    DROP COLUMN IF EXISTS reserved_disk_bytesec,
    ADD COLUMN IF NOT EXISTS used_disk_bytesec UInt64 DEFAULT 0 COMMENT 'BILLABLE integral: ACTUAL disk byte-seconds — SUM of disk_bytes (host st_blocks of the CoW overlay, which reflink-contains the base image content once) across ALL sampled time. No state window, unlike the RAM integral: the overlay persists while running, suspended, hibernated AND stopped, and disk bills in every non-destroyed state.' AFTER used_ram_bytesec_rs;

-- Recreate the MV (definition only; data untouched). Column order mirrors the
-- post-ALTER table exactly (position-aligned): used_disk_bytesec keeps the old
-- reserved integral's slot, sourced from disk_bytes; active_used_seconds stays
-- last (012).
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
    -- reserved_vcpu is usage.-qualified: the max(...) AS reserved_* aliases
    -- above shadow the raw columns (unqualified => nested aggregation error).
    -- memory_bytes and disk_bytes are NOT shadowed (their aliases are
    -- mem_avg_bytes / mem_peak_bytes / disk_peak_bytes) so they need no
    -- qualifier. reserved_disk_bytes is no longer summed at all (014).
    sumIf(usage.reserved_vcpu, state = 'running')              AS reserved_vcpu_sec_run,
    sumIf(memory_bytes, state IN ('running', 'suspended'))     AS used_ram_bytesec_rs,
    sum(disk_bytes)                                            AS used_disk_bytesec,
    sum(net_rx_delta_bytes)                                    AS net_rx_bytes,
    sum(net_tx_delta_bytes)                                    AS net_tx_bytes,
    toUInt32(countIf(cpu_delta_us > 50000 OR net_rx_delta_bytes + net_tx_delta_bytes > 1000)) AS active_used_seconds
FROM usage FINAL
WHERE timestamp >= toStartOfHour(now()) - INTERVAL 3 HOUR
GROUP BY hour, user_id, vm_id, vm_name;
