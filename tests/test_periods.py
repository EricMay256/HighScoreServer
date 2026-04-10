import pytest
from datetime import datetime, timezone
from app.periods import get_period_start, PERIODS


# ── Helpers ────────────────────────────────────────────────────────────────

def utc(year, month, day, hour=0, minute=0, second=0) -> datetime:
    """Convenience constructor for UTC datetimes."""
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


# ── alltime ────────────────────────────────────────────────────────────────

def test_alltime_returns_fixed_sentinel():
    result = get_period_start("alltime")
    assert result == utc(2000, 1, 1)


def test_alltime_ignores_at_parameter():
    """The sentinel should be the same regardless of when it's called."""
    result = get_period_start("alltime", at=utc(2024, 6, 15, 14, 30))
    assert result == utc(2000, 1, 1)


# ── daily ──────────────────────────────────────────────────────────────────

def test_daily_floors_to_midnight():
    at = utc(2024, 6, 15, 14, 30, 45)
    result = get_period_start("daily", at=at)
    assert result == utc(2024, 6, 15, 0, 0, 0)


def test_daily_already_at_midnight():
    at = utc(2024, 6, 15, 0, 0, 0)
    result = get_period_start("daily", at=at)
    assert result == utc(2024, 6, 15, 0, 0, 0)


def test_daily_strips_microseconds():
    at = datetime(2024, 6, 15, 14, 30, 45, 123456, tzinfo=timezone.utc)
    result = get_period_start("daily", at=at)
    assert result.microsecond == 0


# ── weekly ─────────────────────────────────────────────────────────────────

def test_weekly_rolls_back_to_monday():
    at = utc(2024, 6, 19)  # Wednesday
    result = get_period_start("weekly", at=at)
    assert result == utc(2024, 6, 17)  # Monday


def test_weekly_on_monday_returns_same_day():
    at = utc(2024, 6, 17)  # Already Monday
    result = get_period_start("weekly", at=at)
    assert result == utc(2024, 6, 17)


def test_weekly_on_sunday_rolls_back_six_days():
    at = utc(2024, 6, 23)  # Sunday
    result = get_period_start("weekly", at=at)
    assert result == utc(2024, 6, 17)  # Previous Monday


def test_weekly_floors_to_midnight():
    at = utc(2024, 6, 19, 23, 59, 59)  # Wednesday, late
    result = get_period_start("weekly", at=at)
    assert result == utc(2024, 6, 17, 0, 0, 0)


def test_weekly_strips_microseconds():
    at = datetime(2024, 6, 19, 14, 30, 45, 999999, tzinfo=timezone.utc)
    result = get_period_start("weekly", at=at)
    assert result.microsecond == 0


# ── return type ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("period", PERIODS)
def test_result_is_utc_aware(period):
    result = get_period_start(period, at=utc(2024, 6, 15, 12, 0))
    assert result.tzinfo is not None
    assert result.utcoffset().total_seconds() == 0


# ── error handling ─────────────────────────────────────────────────────────

def test_unknown_period_raises_value_error():
    with pytest.raises(ValueError, match="Unknown period"):
        get_period_start("monthly")


def test_unknown_period_message_includes_period_name():
    with pytest.raises(ValueError, match="monthly"):
        get_period_start("monthly")