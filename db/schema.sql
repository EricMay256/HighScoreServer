-- schema.sql
-- Run locally:   psql -U postgres -d leaderboard -f schema.sql
-- Run on Heroku: heroku pg:psql < schema.sql

CREATE TABLE IF NOT EXISTS scores (
    id           SERIAL PRIMARY KEY,
    player       VARCHAR(64)  NOT NULL,
    score        INTEGER      NOT NULL,
    game_mode    VARCHAR(32)  NOT NULL,
    submitted_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (player, game_mode)
);