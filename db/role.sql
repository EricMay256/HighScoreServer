-- role.sql
-- Run locally:   psql -U postgres -d leaderboard -f db/role.sql
-- Do NOT run on production (yet...?)

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
GRANT SELECT, INSERT, UPDATE ON TABLE leaderboard_snapshots TO leaderboard_app;
-- Allow the app to read generated IDs (SERIAL uses a sequence under the hood)
GRANT USAGE, SELECT ON SEQUENCE leaderboard_snapshots_id_seq TO leaderboard_app;
-- The app doesn't need to modify game modes, so only grant SELECT
GRANT SELECT ON TABLE game_modes TO leaderboard_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE users TO leaderboard_app;
GRANT USAGE, SELECT ON SEQUENCE users_id_seq TO leaderboard_app;

GRANT SELECT, INSERT, DELETE ON TABLE refresh_tokens TO leaderboard_app;
GRANT USAGE, SELECT ON SEQUENCE refresh_tokens_id_seq TO leaderboard_app;

