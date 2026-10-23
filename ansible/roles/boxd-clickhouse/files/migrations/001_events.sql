CREATE TABLE IF NOT EXISTS events (
    timestamp DateTime64(3),
    event_type LowCardinality(String),
    user_id String,
    session_id String DEFAULT '',
    vm_id String DEFAULT '',
    vm_name String DEFAULT '',
    node_id String DEFAULT '',
    metadata String DEFAULT '{}',
    INDEX idx_user user_id TYPE bloom_filter GRANULARITY 4,
    INDEX idx_session session_id TYPE bloom_filter GRANULARITY 4,
    INDEX idx_vm vm_id TYPE bloom_filter GRANULARITY 4
) ENGINE = MergeTree()
ORDER BY (event_type, timestamp)
TTL toDateTime(timestamp) + INTERVAL 1 YEAR;
