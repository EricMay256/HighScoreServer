-- schema.sql
-- Run locally:   psql -U postgres -d leaderboard -f schema.sql
-- Run on Heroku: heroku pg:psql < schema.sql

-- If you are REALLY SURE you want to destroy all data, uncomment the 
-- lines below and run this file. If you care about your data I hope 
-- you backed it up!
--
-- DROP TABLE IF EXISTS scores;
-- DROP TABLE IF EXISTS game_modes;

CREATE TABLE IF NOT EXISTS game_modes (
    name        VARCHAR(32)  PRIMARY KEY,
    sort_order  VARCHAR(4) NOT NULL DEFAULT 'DESC'
      CHECK (sort_order IN ('ASC', 'DESC'))
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

