# Leaderboard Server

A FastAPI leaderboard backend for a Unity game, hosted on Heroku.
Provides a high score API with Redis caching.

## Stack

- **Backend:** FastAPI (Python)
- **Database:** PostgreSQL via psycopg2
- **Cache:** Redis (120s TTL on leaderboard reads)
- **Hosting:** Heroku (Postgres + Redis add-ons)
- **Auth:** API key via `x-api-key` header (writes only)

## Local Setup

### Prerequisites
- Python 3.12+
- PostgreSQL
- Redis (or Memurai on Windows)

### Steps

1. Clone the repo and install dependencies:
```bash
   pip install -r requirements.txt
```

2. Copy the example environment file and fill in your values:
```bash
   cp .env.example .env
```

3. Create the local database and schema:
```bash
   psql -U postgres -c "CREATE DATABASE leaderboard;"
   psql -U postgres -d leaderboard -f schema.sql
```

4. Optionally load seed data:
```bash
   psql -U postgres -d leaderboard -f seed.sql
```

5. Start the development server:
```bash
   uvicorn app.main:app --reload
```

6. Visit `http://localhost:8000/docs` to explore the API.

## API Reference

### `GET /api/scores?game_mode={mode}`
Returns the top 100 scores for the given game mode. Public.

### `POST /api/scores`
Submits a score. Requires `x-api-key` header.

Request body:
```json
{
  "player": "alice",
  "score": 1500,
  "game_mode": "classic"
}
```

Upserts on `(player, game_mode)` — only updates if the new score is higher.

## Heroku Deployment
```bash
heroku create your-app-name
heroku addons:create heroku-postgresql:essential-0
heroku addons:create heroku-redis:mini
heroku config:set API_KEY=your-production-secret
heroku pg:psql < schema.sql
git push heroku main
```

## Project Structure
```
leaderboard-server/
├── app/
│   ├── main.py          # App factory, lifespan startup/shutdown
│   ├── models.py        # Pydantic schemas
│   ├── periods.py       # Time intervals for renewing leaderboards
│   ├── api.py           # API endpoints
│   ├── views.py         # HTML endpoints
│   ├── db.py            # psycopg2 connection pool
│   ├── cache.py         # Redis client
│   ├── dependencies.py  # API key auth
│   └── env.py           # Enforces expected env variables
├── db/
│   ├── schema.sql       # Database Schema
│   ├── seed.sql         # Local test data
│   └── role.sql         # Configures role for minimalistic user permissions
├── public/
│   ├── index.html       # Placeholder to receive empty URL requests
│   └── style.css        # CSS for templates
├── requirements.txt
├── Procfile
├── runtime.txt
└── wsgi.py
```

## Known Future Considerations

Public
- Support Fastest time/lowest move count interactions
- Provide browsing information via API
- Create publicly accessible views of database
- Stronger account support
- Sample code starters (Unity, ?)
Private
- Create multiple profiles segregating game operations
- Implement testing
- Add rate limiting via slowapi
- Add Sentry for error tracking
- Add a schema migration tool (node-pg-migrate or alembic)
- Migrate from psycopg2 to asyncpg if concurrency demands it