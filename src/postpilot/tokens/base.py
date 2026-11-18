from datetime import UTC, datetime
from typing import Protocol

DEFAULT_REFRESH_UNDER_DAYS = 30


def utc_datetime(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required")
    return value.astimezone(UTC)


def days_left(expires_at: datetime, now: datetime) -> float:
    return (utc_datetime(expires_at) - utc_datetime(now)).total_seconds() / 86400


class TokenManager(Protocol):
    provider: str
    refresh_under_days: int = DEFAULT_REFRESH_UNDER_DAYS

    def days_left(self, now: datetime) -> float | None: ...
    def try_refresh(self) -> bool: ...
    def renewal_url(self, state: str) -> str: ...
    def complete_renewal(self, code: str) -> None: ...
