-- Projections that sort `events` and `sessions` by user, for per-user queries.

ALTER TABLE events
    ADD PROJECTION IF NOT EXISTS by_user (
        SELECT * ORDER BY (user_id, timestamp)
    );

ALTER TABLE events MATERIALIZE PROJECTION by_user;

ALTER TABLE sessions MODIFY SETTING deduplicate_merge_projection_mode = 'rebuild';

ALTER TABLE sessions
    ADD PROJECTION IF NOT EXISTS by_user (
        SELECT * ORDER BY (user_id, started_at)
    );

ALTER TABLE sessions MATERIALIZE PROJECTION by_user;
