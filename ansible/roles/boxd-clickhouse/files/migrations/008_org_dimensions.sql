-- Organization dimension tables, so analytics can resolve the org_id stamped on
-- usage (007) to a human org, and answer who owns / admins / belongs to an org.
--
-- Kept in sync from the authoritative state.db (organizations + org_members) by
-- a leader-gated reconcile loop in boxd-control (org_sync), which upserts the
-- current rows and tombstones (is_deleted=1) anything removed from state.db.
-- Both are ReplacingMergeTree(updated_at, is_deleted): reads use FINAL and
-- filter is_deleted=0. Mirrors the existing `users` dimension pattern (same
-- JSONEachRow writer), just timer-triggered instead of login-triggered so it
-- backfills existing orgs and self-heals.

-- Org catalog: org_id -> name + the single accountable owner.
CREATE TABLE IF NOT EXISTS organizations (
    org_id String COMMENT 'Org UUID (org-...). Matches usage.org_id / usage_hourly.org_id.',
    name String DEFAULT '' COMMENT 'Human org name (unique in state.db).',
    owner_user_id String DEFAULT '' COMMENT 'The one accountable owner (organizations.owner_user_id).',
    created_at DateTime64(3) DEFAULT 0 COMMENT 'Org creation time.',
    updated_at DateTime64(3) COMMENT 'Reconcile write time; ReplacingMergeTree keeps the latest per org_id.',
    is_deleted UInt8 DEFAULT 0 COMMENT '1 = org destroyed in state.db (tombstone). Filter is_deleted=0.'
) ENGINE = ReplacingMergeTree(updated_at, is_deleted)
ORDER BY org_id;

-- Roster: one row per (org, user) with role. The owner is synthesized here as
-- role='owner' (in state.db the owner lives on organizations, not org_members),
-- so this single table holds everyone: owner + admins + members.
CREATE TABLE IF NOT EXISTS org_members (
    org_id String COMMENT 'Org UUID.',
    user_id String COMMENT 'Member user_id (join to users.user_id for github_username).',
    role LowCardinality(String) DEFAULT 'member' COMMENT '''owner'' | ''admin'' | ''member''.',
    updated_at DateTime64(3) COMMENT 'Reconcile write time; latest wins per (org_id,user_id).',
    is_deleted UInt8 DEFAULT 0 COMMENT '1 = membership removed / org destroyed. Filter is_deleted=0.'
) ENGINE = ReplacingMergeTree(updated_at, is_deleted)
ORDER BY (org_id, user_id);
