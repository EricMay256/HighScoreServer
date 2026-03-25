-- seed.sql
-- Run locally: psql -U postgres -d leaderboard -f seed.sql
-- Do NOT run on production

INSERT INTO scores (player, score, game_mode) VALUES
    ('alice',   1500, 'classic'),
    ('bob',     1200, 'classic'),
    ('charlie',  900, 'classic'),
    ('alice',    800, 'endless'),
    ('bob',     2100, 'endless')
ON CONFLICT (player, game_mode) DO NOTHING;