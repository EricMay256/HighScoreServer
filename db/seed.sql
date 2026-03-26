-- seed.sql
-- Run locally: psql -U postgres -d leaderboard -f db/seed.sql
-- Do NOT run on production
INSERT INTO game_modes (name, sort_order, label) VALUES
    ('classic', 'DESC', 'Classic Mode'),
    ('endless', 'DESC', 'Endless Mode'),
    ('speedrun', 'ASC', 'Speedrun Mode');

INSERT INTO scores (player, score, game_mode) VALUES
    ('alice',   1500, 'classic'),
    ('bob',     1200, 'classic'),
    ('charlie',  900, 'classic'),
    ('alice',    800, 'endless'),
    ('bob',     2100, 'endless')
ON CONFLICT (player, game_mode) DO NOTHING; -- Instead of failing violently