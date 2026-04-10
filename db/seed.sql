-- seed.sql
-- Run locally:   psql -U postgres -d leaderboard -f db/seed.sql
-- Run on Heroku: cat db/seed.sql | heroku pg:psql --app high-score-server

INSERT INTO users (username, email, password_hash, is_guest) VALUES
    ('alice',   'alice@example.com',   '$2b$04$COuT3ESMTH4HY9HgxtkrdeKKKaTnjpx3JB5fdMJPEo5JC/WBhY5wi', FALSE),
    ('bob',     'bob@example.com',     '$2b$04$COuT3ESMTH4HY9HgxtkrdeKKKaTnjpx3JB5fdMJPEo5JC/WBhY5wi', FALSE),
    ('charlie', 'charlie@example.com', '$2b$04$COuT3ESMTH4HY9HgxtkrdeKKKaTnjpx3JB5fdMJPEo5JC/WBhY5wi', FALSE),
    ('cosmo',   'cosmo@example.com',   '$2b$04$COuT3ESMTH4HY9HgxtkrdeKKKaTnjpx3JB5fdMJPEo5JC/WBhY5wi', FALSE),
    ('zfg',     'zfg@example.com',     '$2b$04$COuT3ESMTH4HY9HgxtkrdeKKKaTnjpx3JB5fdMJPEo5JC/WBhY5wi', FALSE)
ON CONFLICT (username) DO NOTHING;

INSERT INTO game_modes (name, sort_order, label, requires_auth) VALUES
    ('classic', 'DESC', 'Classic Mode', FALSE),
    ('speedrun', 'ASC', 'Speedrun Mode', FALSE),
    ('challenge', 'DESC', 'Challenge Mode', TRUE)
ON CONFLICT (name) DO NOTHING;

INSERT INTO leaderboard_snapshots (score, game_mode, period, period_start, submitted_at, user_id) VALUES
    (1500, 'classic', 'alltime', '2000-01-01 00:00:00+00', '2023-01-01 00:00:00+00', (SELECT id FROM users WHERE username = 'alice')),
    (1200, 'classic', 'alltime', '2000-01-01 00:00:00+00', '2023-01-02 00:00:00+00', (SELECT id FROM users WHERE username = 'bob')),
    (1800, 'classic', 'alltime', '2000-01-01 00:00:00+00', '2023-01-03 00:00:00+00', (SELECT id FROM users WHERE username = 'charlie')),
    (900,  'speedrun', 'alltime', '2000-01-01 00:00:00+00', '2023-01-04 00:00:00+00', (SELECT id FROM users WHERE username = 'cosmo')),
    (1100, 'speedrun', 'alltime', '2000-01-01 00:00:00+00', '2023-01-05 00:00:00+00', (SELECT id FROM users WHERE username = 'zfg'))
ON CONFLICT DO NOTHING;
