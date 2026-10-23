-- Org attribution on the usage facts. Each per-second `usage` row is stamped at
-- WRITE time (in the worker's analytics loop) with the VM's billing org and how
-- it's attached, so usage is attributed to the org that owned the VM AT THE
-- SAMPLE. This is correct across `MoveVmToOrg` moves and share/unshare
-- transitions — a join to the *current* vm→org mapping would retroactively
-- re-bill a VM's whole history to whichever org it most recently moved to.
--
--   org_id   = billing_org() = vms.org_id.or(vms.quota_org_id); '' = personal.
--   org_link = 'shared'  (VM shared into the org — vms.org_id set)
--            | 'private' (private VM billed to the org — vms.quota_org_id set)
--            | ''        (personal — no org).
--
-- Forward-only: existing rows keep org_id='' (we never recorded point-in-time
-- VM→org history, and a VM can move orgs, so back-filling from the current
-- mapping would be wrong). The columns land AFTER user_id for readability;
-- `usage` is written via JSONEachRow (matched by name) so order is cosmetic.
ALTER TABLE usage
    ADD COLUMN IF NOT EXISTS org_id   LowCardinality(String) DEFAULT '' AFTER user_id,
    ADD COLUMN IF NOT EXISTS org_link LowCardinality(String) DEFAULT '' AFTER org_id;

ALTER TABLE usage_hourly
    ADD COLUMN IF NOT EXISTS org_id   LowCardinality(String) DEFAULT '' AFTER user_id,
    ADD COLUMN IF NOT EXISTS org_link LowCardinality(String) DEFAULT '' AFTER org_id;

-- The refreshable MV can't be ALTERed, so drop + recreate it to carry
-- org_id/org_link. The sort key is UNCHANGED (user_id, vm_id, hour), so the
-- 3-hour re-emit still REPLACES prior rows (ReplacingMergeTree) — no
-- double-count on upgrade. For the rare VM that changes org mid-hour we take the
-- org it ENDED the hour in (argMax by timestamp); the exact per-second split
-- always remains in `usage`. (This DROP+CREATE re-runs on every ansible pass;
-- it only rebuilds the view definition — the usage_hourly data is untouched.)
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
