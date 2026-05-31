-- role.sql
--
-- Source of truth for the least-privilege leaderboard_app role and its grants.
-- Grants are deliberately NOT in Alembic migrations: migrations manage the
-- production schema, but this role exists only where the platform can host a
-- restricted credential (dev / CI / local). The production database is a
-- single-dyno Heroku Essential-tier plan, which exposes only the default owner
-- credential and cannot create additional roles, so it runs as the owner (which
-- holds all privileges implicitly) and never executes this file. See ADR 0009.
--
-- Run locally:   psql -U postgres -d leaderboard -f db/role.sql
-- This is dev-enforced (a missing grant fails loudly when the app connects as
-- leaderboard_app) and prod-documentary (kept accurate for any future
-- environment that can host the restricted role).

-- You are encouraged to replace the password and role below.
-- It is granted permission to read and write individual scores, but nothing too destructive.
-- NOTE: PostgreSQL does not support "CREATE ROLE IF NOT EXISTS", so we use a DO block.
DO
$$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'leaderboard_app') THEN
        -- Replace this placeholder password before using in any real environment.
        CREATE ROLE leaderboard_app WITH LOGIN PASSWORD 'REPLACE_WITH_SECURE_PASSWORD';
    END IF;
END
$$;
-- Grant access to the database
GRANT CONNECT ON DATABASE leaderboard TO leaderboard_app;

-- Connect to the leaderboard database to grant table-level permissions
\c leaderboard

-- Grant only what the app actually needs
GRANT USAGE ON SCHEMA public TO leaderboard_app;
-- Players may retrieve, submit, and update scores. Auth is TODO but this avoids dropping a table.
GRANT SELECT, INSERT, UPDATE ON TABLE scores TO leaderboard_app;
-- Allow the app to read generated IDs (SERIAL uses a sequence under the hood)
GRANT USAGE, SELECT ON SEQUENCE scores_id_seq TO leaderboard_app;
-- The app doesn't need to modify game modes, so only grant SELECT
GRANT SELECT ON TABLE game_modes TO leaderboard_app;

GRANT SELECT, INSERT, UPDATE ON TABLE users TO leaderboard_app;
GRANT USAGE, SELECT ON SEQUENCE users_id_seq TO leaderboard_app;

GRANT SELECT, INSERT, DELETE ON TABLE refresh_tokens TO leaderboard_app;
GRANT USAGE, SELECT ON SEQUENCE refresh_tokens_id_seq TO leaderboard_app;

-- Validated runs (see specs.md Phase 1). The app inserts a pending run and
-- later UPDATEs it with the server-computed canonical_score/tier/status, so it
-- needs SELECT, INSERT, UPDATE — but not DELETE (runs are leaderboard history,
-- never pruned). SERIAL id needs the sequence.
GRANT SELECT, INSERT, UPDATE ON TABLE runs TO leaderboard_app;
GRANT USAGE, SELECT ON SEQUENCE runs_id_seq TO leaderboard_app;

-- Cumulative-submission dedup markers. Written once via
-- INSERT ... ON CONFLICT DO NOTHING and read back; never UPDATEd. DELETE is
-- granted only because scripts/prune_idempotency_keys.py reaps old rows
-- (matching how prune_guests is privileged on users). No sequence: the table's
-- primary key is the composite (user_id, game_mode, key), not a serial.
GRANT SELECT, INSERT, DELETE ON TABLE submission_idempotency TO leaderboard_app;

