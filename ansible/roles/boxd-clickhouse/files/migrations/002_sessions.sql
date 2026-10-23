CREATE TABLE IF NOT EXISTS sessions (
    session_id String,
    user_id String,
    session_type LowCardinality(String),
    vm_id String DEFAULT '',
    vm_name String DEFAULT '',
    node_id String DEFAULT '',
    started_at DateTime64(3),
    last_seen_at DateTime64(3),
    ended_at Nullable(DateTime64(3)),
    metadata String DEFAULT '{}',
    INDEX idx_user user_id TYPE bloom_filter GRANULARITY 4
) ENGINE = ReplacingMergeTree(last_seen_at)
ORDER BY (session_id)
TTL toDateTime(started_at) + INTERVAL 1 YEAR;
