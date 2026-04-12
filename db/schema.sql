-- schema.sql
-- Run locally:   psql -U postgres -d leaderboard -f db/schema.sql
-- Run on Heroku: cat db/schema.sql | heroku pg:psql --app high-score-server

-- If you are REALLY SURE you want to destroy all data, uncomment the 
-- lines below and run this file. If you care about your data I hope 
-- you backed it up!
--
-- Recommended drop order due to foreign key usage:
-- DROP TABLE IF EXISTS scores;
-- DROP TABLE IF EXISTS game_modes;
-- DROP TABLE IF EXISTS refresh_tokens;
-- DROP TABLE IF EXISTS users;

CREATE TABLE IF NOT EXISTS game_modes (
    name          VARCHAR(32)  PRIMARY KEY,
    sort_order    VARCHAR(4) NOT NULL DEFAULT 'DESC'
      CHECK (sort_order IN ('ASC', 'DESC')),
    label         TEXT,
    requires_auth BOOLEAN      NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      VARCHAR(64)  UNIQUE NOT NULL,
    email         VARCHAR(256) UNIQUE,
    password_hash TEXT         ,
    is_guest      BOOLEAN      NOT NULL DEFAULT FALSE,
    is_verified   BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email
    ON users (email) WHERE email IS NOT NULL;


CREATE TABLE IF NOT EXISTS refresh_tokens (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash    TEXT         UNIQUE NOT NULL,
    expires_at    TIMESTAMPTZ  NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
-- No current reason to index refresh tokens by user_id
-- But if we wish to invalidate all sessions for a user we will want it
-- CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user
--     ON refresh_tokens (user_id);


CREATE TABLE IF NOT EXISTS scores (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER      NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    game_mode    VARCHAR(32)  NOT NULL REFERENCES game_modes(name),
    score        BIGINT      NOT NULL,
    period       VARCHAR(16)  NOT NULL,  -- 'alltime', 'weekly', 'daily'
    period_start TIMESTAMPTZ  NOT NULL,  -- start of the window this score belongs to
    submitted_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, game_mode, period, period_start)
);
CREATE INDEX IF NOT EXISTS idx_scores_lookup_desc
    ON scores (game_mode, period, period_start, score DESC, submitted_at ASC, id ASC);
CREATE INDEX IF NOT EXISTS idx_scores_lookup_asc
    ON scores (game_mode, period, period_start, score ASC, submitted_at ASC, id ASC);
