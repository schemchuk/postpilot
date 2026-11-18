from datetime import UTC, datetime, timedelta, timezone

import pytest

from postpilot.tokens.service import check_tokens

NOW = datetime(2026, 11, 18, 23, 30, tzinfo=UTC)


class FakeManager:
    def __init__(self, provider, remaining, *, window=30, result=False):
        self.provider = provider
        self.remaining = remaining
        self.refresh_under_days = window
        self.result = result
        self.refreshes = 0

    def days_left(self, now):
        return self.remaining

    def try_refresh(self):
        self.refreshes += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def renewal_url(self, state):
        return "/renew"

    def complete_renewal(self, code):
        pass


class MemoryState:
    def __init__(self):
        self.days = {}

    def send_token_alert_once(self, provider, day, send):
        if provider not in self.days or self.days[provider] < day:
            send()
            self.days[provider] = day


@pytest.mark.parametrize(
    ("remaining", "window", "result", "status", "refreshes", "alert"),
    [
        (None, 30, False, "missing", 0, "token missing"),
        (0, 30, False, "expired", 0, "token expired, items will be held"),
        (-1, 30, False, "expired", 0, "token expired, items will be held"),
        (7, 0, False, "expiring", 0, "token expires in 7 days"),
        (7.1, 0, False, "healthy", 0, None),
        (30, 30, True, "refreshed", 1, None),
        (30.1, 30, False, "healthy", 0, None),
        (0.1, 30, True, "refreshed", 1, None),
        (0.1, 30, False, "refresh_failed", 1, "token expires in 1 day"),
        (5, 30, False, "refresh_failed", 1, "token expires in 5 days"),
        (20, 30, False, "refresh_failed", 1, None),
    ],
)
def test_token_status(remaining, window, result, status, refreshes, alert):
    manager = FakeManager("provider", remaining, window=window, result=result)
    messages = []
    report = check_tokens([manager], messages.append, MemoryState(), NOW)
    assert report.statuses == {"provider": status}
    assert manager.refreshes == refreshes
    assert messages == ([] if alert is None else [f"provider: {alert}"])


def test_alerts_deduplicated_by_provider_and_utc_day():
    first = FakeManager("first", None)
    second = FakeManager("second", -1)
    messages = []
    state = MemoryState()
    check_tokens([first, second], messages.append, state, NOW)
    check_tokens(
        [first, second],
        messages.append,
        state,
        NOW.astimezone(timezone(timedelta(hours=2))),
    )
    assert len(messages) == 2
    check_tokens([first, second], messages.append, state, NOW + timedelta(hours=1))
    assert messages[:2] == messages[2:]
    assert len(messages) == 4


def test_refresh_exception_is_redacted_and_other_providers_continue(caplog):
    private_value = "fake private token"
    broken = FakeManager("broken", 2, result=RuntimeError(private_value))
    working = FakeManager("working", 3, result=True)
    messages = []
    report = check_tokens([broken, working], messages.append, MemoryState(), NOW)
    assert report.statuses == {"broken": "refresh_failed", "working": "refreshed"}
    assert messages == ["broken: token expires in 2 days"]
    assert "RuntimeError" in caplog.text
    assert private_value not in caplog.text
    assert private_value not in " ".join(messages)


def test_notifier_failure_is_redacted_and_retried_without_blocking_others(caplog):
    managers = [FakeManager("first", None), FakeManager("second", -1)]
    state = MemoryState()
    messages = []
    private_value = "fake private notifier payload"

    def send(message):
        if message.startswith("first:"):
            raise RuntimeError(private_value)
        messages.append(message)

    report = check_tokens(managers, send, state, NOW)
    assert report.statuses == {"first": "missing", "second": "expired"}
    assert messages == ["second: token expired, items will be held"]
    assert state.days == {"second": NOW.date()}
    assert "RuntimeError" in caplog.text
    assert private_value not in caplog.text

    check_tokens(managers, messages.append, state, NOW)
    assert messages == [
        "second: token expired, items will be held",
        "first: token missing",
    ]
