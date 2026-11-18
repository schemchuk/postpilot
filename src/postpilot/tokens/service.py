import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from math import ceil
from typing import Literal, Protocol

from postpilot.tokens.base import TokenManager, utc_datetime

logger = logging.getLogger(__name__)

type TokenStatus = Literal[
    "missing", "healthy", "expiring", "expired", "refreshed", "refresh_failed"
]


class AlertState(Protocol):
    def send_token_alert_once(
        self, provider: str, day: date, send: Callable[[], None]
    ) -> None: ...


@dataclass(frozen=True)
class TokenReport:
    statuses: dict[str, TokenStatus]


def _check_token(
    manager: TokenManager, now: datetime
) -> tuple[TokenStatus, str | None]:
    remaining = manager.days_left(now)
    if remaining is None:
        return "missing", "token missing"
    if remaining <= 0:
        return "expired", "token expired, items will be held"

    days = ceil(remaining)
    unit = "day" if days == 1 else "days"
    expiry_alert = f"token expires in {days} {unit}"
    if remaining <= manager.refresh_under_days:
        try:
            if manager.try_refresh():
                return "refreshed", None
        except Exception as error:  # noqa: BLE001 - isolate provider refresh failures
            logger.warning("Token refresh failed (%s)", type(error).__name__)
        return "refresh_failed", expiry_alert if remaining <= 7 else None
    if remaining <= 7:
        return "expiring", expiry_alert
    return "healthy", None


def check_tokens(
    managers: Iterable[TokenManager],
    notifier_send: Callable[[str], None],
    state: AlertState,
    now: datetime,
) -> TokenReport:
    now = utc_datetime(now)
    statuses = {}
    for manager in managers:
        status, alert = _check_token(manager, now)
        provider = manager.provider
        statuses[provider] = status
        if alert is not None:
            message = f"{provider}: {alert}"
            try:
                state.send_token_alert_once(
                    provider, now.date(), partial(notifier_send, message)
                )
            except Exception as error:  # noqa: BLE001 - isolate alert failures
                logger.warning("Token alert failed (%s)", type(error).__name__)
    return TokenReport(statuses)
