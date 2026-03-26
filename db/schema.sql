-- schema.sql
-- Run locally:   psql -U postgres -d leaderboard -f db/schema.sql
-- Run on Heroku: heroku pg:psql < schema.sql

-- If you are REALLY SURE you want to destroy all data, uncomment the 
-- lines below and run this file. If you care about your data I hope 
-- you backed it up!
--
 DROP TABLE IF EXISTS scores;
 DROP TABLE IF EXISTS game_modes;

CREATE TABLE IF NOT EXISTS game_modes (
    name        VARCHAR(32)  PRIMARY KEY,
    sort_order  VARCHAR(4) NOT NULL DEFAULT 'DESC'
      CHECK (sort_order IN ('ASC', 'DESC')),
    label       TEXT
);

CREATE TABLE IF NOT EXISTS scores (
    id           SERIAL PRIMARY KEY,
    player       VARCHAR(64)  NOT NULL,
    score        INTEGER      NOT NULL,
    game_mode    VARCHAR(32)  NOT NULL REFERENCES game_modes(name),
    submitted_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (player, game_mode)
);

-- You are encouraged to replace the password and role below
-- It is granted permission to read and write individual scores, but nothing too destructive
CREATE ROLE IF NOT EXISTS leaderboard_app WITH LOGIN PASSWORD 'leaderboard_pass';
-- Grant access to the database
GRANT CONNECT ON DATABASE leaderboard TO leaderboard_app;

-- Connect to the leaderboard database to grant table-level permissions
\c leaderboard

-- Grant only what the app actually needs
GRANT USAGE ON SCHEMA public TO leaderboard_app;
GRANT SELECT, INSERT, UPDATE ON TABLE scores TO leaderboard_app;
-- The app doesn't need game modes yet but can get them here
-- GRANT SELECT ON TABLE game_modes TO leaderboard_app;