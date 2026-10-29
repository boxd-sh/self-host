-- Org and org-member dimension tables, kept in sync with the control plane's state.

CREATE TABLE IF NOT EXISTS organizations (
    org_id String COMMENT 'Org UUID (org-...). Matches usage.org_id / usage_hourly.org_id.',
    name String DEFAULT '' COMMENT 'Human org name (unique in state.db).',
    owner_user_id String DEFAULT '' COMMENT 'The one accountable owner (organizations.owner_user_id).',
    created_at DateTime64(3) DEFAULT 0 COMMENT 'Org creation time.',
    updated_at DateTime64(3) COMMENT 'Reconcile write time; ReplacingMergeTree keeps the latest per org_id.',
    is_deleted UInt8 DEFAULT 0 COMMENT '1 = org destroyed in state.db (tombstone). Filter is_deleted=0.'
) ENGINE = ReplacingMergeTree(updated_at, is_deleted)
ORDER BY org_id;

CREATE TABLE IF NOT EXISTS org_members (
    org_id String COMMENT 'Org UUID.',
    user_id String COMMENT 'Member user_id (join to users.user_id for github_username).',
    role LowCardinality(String) DEFAULT 'member' COMMENT '''owner'' | ''admin'' | ''member''.',
    updated_at DateTime64(3) COMMENT 'Reconcile write time; latest wins per (org_id,user_id).',
    is_deleted UInt8 DEFAULT 0 COMMENT '1 = membership removed / org destroyed. Filter is_deleted=0.'
) ENGINE = ReplacingMergeTree(updated_at, is_deleted)
ORDER BY (org_id, user_id);
