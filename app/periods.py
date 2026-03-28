from datetime import datetime, timezone, timedelta


def get_period_start(period: str, at: datetime | None = None) -> datetime:
    """
    Return the start of the current window for the given period.
    All windows are anchored to UTC.
    """
    now = at or datetime.now(timezone.utc)

    if period == "alltime":
        # Sentinel — a fixed epoch so the UNIQUE constraint still works
        return datetime(2000, 1, 1, tzinfo=timezone.utc)
    elif period == "daily":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "weekly":
        # Weeks start on Monday
        monday = now - timedelta(days=now.weekday())
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"Unknown period: {period!r}")

# coupled with app/models.py:LeaderboardQuery.period and app/api.py submit_score loop
PERIODS = ("alltime", "daily", "weekly")