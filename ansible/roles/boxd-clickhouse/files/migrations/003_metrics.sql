CREATE TABLE IF NOT EXISTS metrics (
    timestamp DateTime64(3),
    user_id String,
    vm_id String,
    vm_name String DEFAULT '',
    node_id String DEFAULT '',
    cpu_us UInt64 DEFAULT 0,
    memory_bytes UInt64 DEFAULT 0,
    disk_bytes UInt64 DEFAULT 0,
    INDEX idx_user user_id TYPE bloom_filter GRANULARITY 4,
    INDEX idx_vm vm_id TYPE bloom_filter GRANULARITY 4
) ENGINE = MergeTree()
ORDER BY (user_id, vm_id, timestamp)
TTL toDateTime(timestamp) + INTERVAL 1 HOUR;
