from datetime import date
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from psycopg import Error

from postpilot.notify.approval import ApprovalHandler
from postpilot.sources.github import GitHubClient
from postpilot.sources.series import (
    ActiveSeriesExists,
    NoActiveSeries,
    QueuedSeriesExists,
    Series,
)

USER = 321
CHAT = 456
USAGE = "Usage: /series_start <repo> [parts (2-10)]"
NEXT_USAGE = "Usage: /series_next <repo> [parts (2-10)]"


def series(total):
    return Series(uuid4(), "alpha", 3, total, date(2026, 11, 20), "active")


@pytest.fixture
def bot():
    api = Mock()
    store = Mock()
    store.get_active.return_value = None
    store.get_queued.return_value = None
    store.posted_repos.return_value = set()
    metadata = {
        "name": "alpha",
        "owner": {"login": "Owner"},
        "visibility": "public",
        "html_url": "https://github.com/owner/alpha",
    }

    def respond(request):
        assert request.url.path == "/repos/owner/alpha"
        return httpx.Response(metadata.get("status", 200), json=metadata)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        factory = Mock(return_value=GitHubClient("owner", client=client))
        handler = ApprovalHandler(
            Mock(),
            api,
            USER,
            series=store,
            github_factory=factory,
            github_owner="owner",
        )

        def send(text, sender=USER):
            handler.handle_update(
                {
                    "message": {
                        "text": text,
                        "chat": {"id": CHAT},
                        "from": {"id": sender},
                    }
                }
            )
            return api.send_message

        yield send, store, metadata, factory


@pytest.mark.parametrize(("total", "expected"), [(5, "3/5"), (None, "3")])
def test_active_series(bot, total, expected):
    send, store, _, factory = bot
    store.get_active.return_value = series(total)
    send("/series@my_bot").assert_called_once_with(
        CHAT, f"Active series: alpha, next part {expected}"
    )
    factory.assert_not_called()


@pytest.mark.parametrize("posted", [set(), {"b", "a"}])
def test_no_active_series(bot, posted):
    send, store, _, _ = bot
    store.posted_repos.return_value = posted
    expected = "No active series.\n"
    if posted:
        expected += "Posted repos: a, b.\n"
    send("/series").assert_called_once_with(
        CHAT, expected + "Start one with /series_start <repo> [parts]."
    )


@pytest.mark.parametrize(
    ("args", "parts", "label"),
    [
        ("alpha 2", 2, "2 parts"),
        ("alpha 10", 10, "10 parts"),
        ("alpha", None, "open-ended"),
    ],
)
def test_start_series(bot, args, parts, label):
    send, store, _, _ = bot
    store.start.return_value = series(parts)
    send(f"/series_start@my_bot {args}").assert_called_once_with(
        CHAT, f"Series started: alpha, {label}"
    )
    store.start.assert_called_once_with("alpha", parts)


@pytest.mark.parametrize(
    "args", ["", "x/y", "x" * 101, "alpha 1", "alpha 11", "alpha x", "alpha 2 extra"]
)
def test_invalid_series_start(bot, args):
    send, store, _, factory = bot
    send(f"/series_start {args}").assert_called_once_with(CHAT, USAGE)
    factory.assert_not_called()
    store.start.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"status": 404},
        {"visibility": "private"},
        {"private": True},
        {"fork": True},
        {"archived": True},
        {"owner": {"login": "elsewhere"}},
    ],
)
def test_ineligible_series_repo(bot, changes):
    send, store, metadata, _ = bot
    metadata.update(changes)
    send("/series_start alpha").assert_called_once_with(CHAT, USAGE)
    store.start.assert_not_called()


def test_series_github_error(bot):
    send, store, metadata, _ = bot
    metadata["status"] = 503
    send("/series_start alpha").assert_called_once_with(
        CHAT, "GitHub check failed, series not started."
    )
    store.start.assert_not_called()


@pytest.mark.parametrize("concurrent_start", [False, True])
def test_conflicting_active_series(bot, concurrent_start):
    send, store, _, factory = bot
    active = series(3)
    if concurrent_start:
        store.get_active.side_effect = [None, active]
        store.start.side_effect = ActiveSeriesExists
    else:
        store.get_active.return_value = active
    send("/series_start alpha").assert_called_once_with(
        CHAT,
        "A series about alpha is already active. Stop it with /series_stop"
        " or queue the next one with /series_next.",
    )
    assert factory.called is concurrent_start


@pytest.mark.parametrize("active", [True, False])
def test_stop_series(bot, active):
    send, store, _, factory = bot
    store.stop.return_value = series(3) if active else None
    send("/series_stop@my_bot").assert_called_once_with(
        CHAT,
        "Series about alpha stopped after part 2." if active else "No active series.",
    )
    factory.assert_not_called()


def test_series_storage_error_replied_to(bot):
    send, store, _, _ = bot
    store.stop.side_effect = Error("private detail")
    send("/series_stop").assert_called_once_with(
        CHAT, "Series storage operation failed."
    )


def test_other_user_cannot_control_series(bot):
    send, store, _, factory = bot
    send("/series_start alpha", sender=999).assert_not_called()
    assert store.mock_calls == factory.mock_calls == []


def queued(total):
    return Series(uuid4(), "alpha", 1, total, date(2026, 11, 21), "queued")


def test_active_series_shows_queue(bot):
    send, store, _, _ = bot
    store.get_active.return_value = series(5)
    store.get_queued.return_value = queued(None)
    send("/series").assert_called_once_with(
        CHAT, "Active series: alpha, next part 3/5\nQueued next: alpha, open-ended"
    )


@pytest.mark.parametrize(
    ("args", "parts", "label"),
    [("alpha 5", 5, "5 parts"), ("alpha", None, "open-ended")],
)
def test_queue_series(bot, args, parts, label):
    send, store, _, _ = bot
    store.get_active.return_value = series(5)
    store.queue.return_value = queued(parts)
    send(f"/series_next@my_bot {args}").assert_called_once_with(
        CHAT,
        f"Series queued: alpha, {label}. It starts when the active series ends.",
    )
    store.queue.assert_called_once_with("alpha", parts)
    store.start.assert_not_called()


@pytest.mark.parametrize("args", ["", "alpha 1", "alpha 11", "x/y"])
def test_invalid_series_next(bot, args):
    send, store, _, factory = bot
    store.get_active.return_value = series(5)
    send(f"/series_next {args}").assert_called_once_with(CHAT, NEXT_USAGE)
    factory.assert_not_called()
    store.queue.assert_not_called()


def test_ineligible_queued_repo(bot):
    send, store, metadata, _ = bot
    store.get_active.return_value = series(5)
    metadata["fork"] = True
    send("/series_next alpha").assert_called_once_with(CHAT, NEXT_USAGE)
    store.queue.assert_not_called()


@pytest.mark.parametrize("concurrent", [False, True])
def test_series_next_without_active(bot, concurrent):
    send, store, _, factory = bot
    if concurrent:
        store.get_active.return_value = series(5)
        store.queue.side_effect = NoActiveSeries
    send("/series_next alpha").assert_called_once_with(
        CHAT, "No active series. Start one with /series_start <repo> [parts]."
    )
    assert factory.called is concurrent


@pytest.mark.parametrize("concurrent", [False, True])
def test_series_next_already_queued(bot, concurrent):
    send, store, _, factory = bot
    store.get_active.return_value = series(5)
    if concurrent:
        store.get_queued.side_effect = [None, queued(5)]
        store.queue.side_effect = QueuedSeriesExists
    else:
        store.get_queued.return_value = queued(5)
    send("/series_next alpha").assert_called_once_with(
        CHAT,
        "A series about alpha is already queued. Cancel it with /series_next_cancel.",
    )
    assert factory.called is concurrent


@pytest.mark.parametrize("present", [True, False])
def test_cancel_queued_series(bot, present):
    send, store, _, factory = bot
    store.cancel_queued.return_value = queued(5) if present else None
    send("/series_next_cancel").assert_called_once_with(
        CHAT,
        "Queued series about alpha cancelled." if present else "No queued series.",
    )
    factory.assert_not_called()


def test_cancel_queued_series_rejects_args(bot):
    send, store, _, _ = bot
    send("/series_next_cancel now").assert_called_once_with(
        CHAT, "Usage: /series_next_cancel"
    )
    store.cancel_queued.assert_not_called()


def test_stop_starts_queued_series(bot):
    send, store, _, _ = bot
    store.stop.return_value = series(3)
    store.get_active.return_value = Series(
        uuid4(), "beta", 1, 5, date(2026, 11, 21), "active"
    )
    send("/series_stop").assert_called_once_with(
        CHAT, "Series about alpha stopped after part 2.\nNext series started: beta."
    )
