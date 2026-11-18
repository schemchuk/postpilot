from datetime import UTC, datetime, timedelta, timezone

import pytest

from postpilot.tokens.base import days_left

NOW = datetime(2026, 11, 18, tzinfo=UTC)


@pytest.mark.parametrize("days", [-1, 0, 0.5, 7])
def test_days_left(days):
    expires_at = (NOW + timedelta(days=days)).astimezone(timezone(timedelta(hours=2)))
    assert days_left(expires_at, NOW) == days


@pytest.mark.parametrize(
    "args", [(NOW.replace(tzinfo=None), NOW), (NOW, NOW.replace(tzinfo=None))]
)
def test_naive_datetime_is_rejected(args):
    with pytest.raises(ValueError, match="timezone-aware"):
        days_left(*args)
