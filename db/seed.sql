-- seed.sql
-- Run locally: psql -U postgres -d leaderboard -f db/seed.sql
-- Do NOT run on production

SET timezone = 'UTC';

INSERT INTO game_modes (name, sort_order, label) VALUES
    ('classic', 'DESC', 'Classic Mode'),
    ('endless', 'DESC', 'Endless Mode'),
    ('speedrun', 'ASC', 'Speedrun Mode')
ON CONFLICT (name) DO NOTHING;

INSERT INTO leaderboard_snapshots (player, score, game_mode, period, period_start, submitted_at) VALUES
    ('alice',   1500, 'classic', 'alltime', '2000-01-01', '2023-01-01 00:00:00+00'),
    ('alice',   1500, 'classic', 'weekly', '2000-01-01', '2023-01-01 00:00:00+00'),
    ('alice',   1500, 'classic', 'daily', '2000-01-01', '2023-01-01 00:00:00+00'),
    ('bob',     1200, 'classic', 'alltime', '2000-01-01', '2023-01-01 00:00:00+00'),
    ('charlie',  900, 'classic', 'alltime', '2000-01-01', '2023-01-01 00:00:00+00'),
    ('alice',    800, 'endless', 'alltime', '2000-01-01', '2023-01-01 00:00:00+00'),
    ('bob',      700, 'endless', 'alltime', '2000-01-01', '2023-01-01 00:00:00+00'),
    ('charlie',  600, 'endless', 'alltime', '2000-01-01', '2023-01-01 00:00:00+00'),
    ('cosmo',   5959, 'speedrun', 'alltime', '2000-01-01', '2023-01-01 00:00:00+00'),
    ('zfg',     5849,  'speedrun', 'alltime', '2000-01-01', '2023-01-01 00:00:00+00')
ON CONFLICT (player, game_mode, period, period_start) DO NOTHING;