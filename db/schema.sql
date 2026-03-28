-- schema.sql
-- Run locally:   psql -U postgres -d leaderboard -f db/schema.sql
-- Run on Heroku: heroku pg:psql < schema.sql

-- If you are REALLY SURE you want to destroy all data, uncomment the 
-- lines below and run this file. If you care about your data I hope 
-- you backed it up!
--
-- Recommended drop order due to foreign key from leaderboard_snapshots(game_mode) to game_modes(name):
-- DROP TABLE IF EXISTS leaderboard_snapshots;
-- DROP TABLE IF EXISTS game_modes;
CREATE TABLE IF NOT EXISTS game_modes (
    name        VARCHAR(32)  PRIMARY KEY,
    sort_order  VARCHAR(4) NOT NULL DEFAULT 'DESC'
      CHECK (sort_order IN ('ASC', 'DESC')),
    label       TEXT
);

CREATE TABLE IF NOT EXISTS leaderboard_snapshots (
    id           SERIAL PRIMARY KEY,
    player       VARCHAR(64)  NOT NULL,
    score        INTEGER      NOT NULL,
    game_mode    VARCHAR(32)  NOT NULL REFERENCES game_modes(name),
    period       VARCHAR(16)  NOT NULL,  -- 'alltime', 'weekly', 'daily'
    period_start TIMESTAMPTZ  NOT NULL,  -- start of the window this score belongs to
    submitted_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (player, game_mode, period, period_start)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_lookup_desc
    ON leaderboard_snapshots (game_mode, period, period_start, score DESC, submitted_at ASC, id ASC);

CREATE INDEX IF NOT EXISTS idx_snapshots_lookup_asc
    ON leaderboard_snapshots (game_mode, period, period_start, score ASC, submitted_at ASC, id ASC);

-- One Time Migration -- Remove me after next deployment

DROP TABLE IF EXISTS scores; -- legacy table, not used anymore