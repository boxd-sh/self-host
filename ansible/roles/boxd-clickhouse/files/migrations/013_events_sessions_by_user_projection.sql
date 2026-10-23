-- Fixes prod 500s on GET /api/v1/analytics/users/:id/events and
-- GET /api/v1/analytics/kpis (both admin.boxd.sh, first seen 2026-08-03).
--
-- Root cause: `events` is ORDER BY (event_type, timestamp) and `sessions` is
-- ORDER BY (session_id) — neither has user_id in its sort key, only a
-- bloom_filter secondary index (idx_user), which is a probabilistic
-- granule-skip hint, not a seek. Every per-user query (`WHERE user_id = ...`
-- or `WHERE user_id IN (...)`) degenerates into reading most or all of the
-- table. Confirmed directly against prod: the `sessions FINAL WHERE user_id =
-- ... AND started_at BETWEEN ...` query behind /users/:id/events read all
-- 1,621,714 rows of `sessions` for ONE user's 7-day window and hit
-- ClickHouse's default max_rows_to_read (1M):
--   Code: 158. Limit for rows or bytes to read exceeded, max rows: 1.00
--   million, current rows: 1.60 million (TOO_MANY_ROWS)
--
-- Fix: a projection is a second physical copy of the table's rows, kept in
-- sync by ClickHouse on every insert/merge, sorted however the projection's
-- own ORDER BY says. Once materialized, the query planner automatically
-- substitutes it for any query whose WHERE/ORDER BY would benefit — no
-- application-side query changes needed. Both tables are small in prod
-- (events: 289 MiB / 5.5M rows, sessions: 88 MiB / 1.6M rows as of
-- 2026-08-03) against 16 GiB free on clickhouse-0, so the ~2x storage cost of
-- a `SELECT *` projection is cheap here — revisit if either table grows into
-- the double-digit-GiB range before `usage` does (usage is NOT covered by
-- this migration; it's already ORDER BY (user_id, vm_id, timestamp), see
-- 005_usage.sql, so it doesn't have this problem).
--
-- MATERIALIZE PROJECTION re-running on every ansible pass (this file has no
-- version tracking — see boxd-clickhouse/tasks/main.yml, it replays every
-- *.sql on every converge) is expected to be cheap after the first run:
-- ClickHouse skips parts that already carry the projection's current
-- mutation version, so this becomes a fast no-op scan rather than a
-- from-scratch rebuild. Confirm with `SELECT * FROM system.mutations WHERE
-- table IN ('events','sessions') AND NOT is_done` after the first apply —
-- it should drain to empty and stay there on subsequent runs.
--
-- `sessions` is a ReplacingMergeTree (keyed on `last_seen_at`, so a merge can
-- drop/replace rows) — ClickHouse refuses `ADD PROJECTION` on any dedup-style
-- engine under its default `deduplicate_merge_projection_mode = throw` unless
-- told how to keep the projection consistent across those merges:
--   Code: 344. ADD PROJECTION is not supported in ReplacingMergeTree with
--   deduplicate_merge_projection_mode = throw
-- `rebuild` (not `drop`) is the right mode here: `drop` would empty the
-- projection's data for any merged part and leave it stale until the next
-- MATERIALIZE, which defeats the point of a projection this endpoint expects
-- to always be queryable. `rebuild` costs more per merge (it recomputes the
-- projection's rows for the merge's output instead of discarding them), but
-- at this table's size (1.6M rows / 88 MiB) that's noise. `events` is a plain
-- MergeTree — no dedup, no equivalent setting needed.

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
