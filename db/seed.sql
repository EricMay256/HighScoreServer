INSERT INTO users (username, email, password_hash, is_guest) VALUES
    ('alice',   'alice@example.com',   'placeholder', FALSE),
    ('bob',     'bob@example.com',     'placeholder', FALSE),
    ('charlie', 'charlie@example.com', 'placeholder', FALSE),
    ('cosmo',   'cosmo@example.com',   'placeholder', FALSE),
    ('zfg',     'zfg@example.com',     'placeholder', FALSE)
ON CONFLICT (username) DO NOTHING;

INSERT INTO leaderboard_snapshots (score, game_mode, period, period_start, submitted_at, user_id)
SELECT 1500, 'classic', 'alltime', '2000-01-01', '2023-01-01 00:00:00+00', id FROM users WHERE username = 'alice'
ON CONFLICT DO NOTHING;
-- repeat pattern for remaining seed rows