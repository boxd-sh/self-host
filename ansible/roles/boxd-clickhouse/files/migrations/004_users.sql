CREATE TABLE IF NOT EXISTS users (
    user_id String,
    github_username String DEFAULT '',
    github_id Int64 DEFAULT 0,
    email String DEFAULT '',
    display_name String DEFAULT '',
    avatar_url String DEFAULT '',
    updated_at DateTime64(3),
    INDEX idx_email email TYPE bloom_filter GRANULARITY 4
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (user_id);
