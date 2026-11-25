from datetime import date, timedelta
from unittest.mock import Mock

import pytest
import test_store
from psycopg import Error
from psycopg.errors import UniqueViolation

from postpilot.config import Settings
from postpilot.core.states import ItemState
from postpilot.jobs import tasks
from postpilot.sources.series import SeriesStore
from postpilot.store.repo import ItemRepo
from postpilot.store.weekly import WeeklyStore

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn
WEEK = date(2026, 11, 16)


def test_create_weekly_draft_is_atomic(conn):
    weekly = WeeklyStore(conn)
    repo = ItemRepo(conn)
    args = (
        WEEK,
        "project",
        None,
        None,
        {"link_allowed": True},
        "Deutsch",
        "Українська",
    )
    item = weekly.create_drafted(*args)
    assert item.source == "github"
    assert item.state == ItemState.AWAITING_APPROVAL
    assert [(variant.lang, variant.text) for variant in repo.get_variants(item.id)] == [
        ("de", "Deutsch"),
        ("ua", "Українська"),
    ]
    with pytest.raises(UniqueViolation):
        weekly.create_drafted(*args)
    with conn.transaction():
        assert conn.execute("SELECT count(*) FROM items").fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM variants").fetchone() == (2,)
    assert weekly.get(WEEK).item_id == item.id
    assert weekly.get(WEEK).facts == {"link_allowed": True}


def test_expire_older_skips_only_awaiting_github_drafts(conn):
    repo = ItemRepo(conn)
    weekly = WeeklyStore(conn)
    older = repo.create_item("github")
    repo.transition(older.id, ItemState.AWAITING_APPROVAL)
    telegram = repo.create_inbox_item(33, "Українська", "Deutsch")
    new = weekly.create_drafted(
        WEEK + timedelta(days=7), "next", None, None, {}, "de", "ua"
    )
    newer = repo.create_item("github")
    repo.transition(newer.id, ItemState.AWAITING_APPROVAL)
    weekly.expire_older(new.id)
    assert repo.get_item(older.id).state == ItemState.SKIPPED
    for item in (new, telegram, newer):
        assert repo.get_item(item.id).state == ItemState.AWAITING_APPROVAL


def test_publish_due_records_weekly_rows_once_even_when_publishing_fails(
    conn, monkeypatch
):
    weekly = WeeklyStore(conn)
    repo = ItemRepo(conn)
    series_store = SeriesStore(conn)
    series = series_store.start("series", 3)
    items = {}
    weeks = {}
    for offset, name in enumerate(("series", "plain", "failed", "pending")):
        week = WEEK + timedelta(weeks=offset + 2)
        series_id, part = (series.id, 1) if name == "series" else (None, None)
        items[name] = weekly.create_drafted(
            week, name, series_id, part, {}, "German", "Ukrainian"
        )
        weeks[name] = week

    def finish(item, state):
        repo.transition(item.id, ItemState.APPROVED, approved_by=1)
        repo.transition(item.id, ItemState.PUBLISHING)
        repo.transition(item.id, state)

    def publish(*_, **kwargs):
        finish(items["series"], ItemState.PUBLISHED)
        finish(items["plain"], ItemState.PUBLISHED)
        finish(items["failed"], ItemState.FAILED)
        assert [row.week for row in weekly.list_unrecorded_published()] == [
            weeks["series"],
            weeks["plain"],
        ]
        raise RuntimeError("Channel publishing failed")

    settings = Settings("fake-db", "fake-bot", 123, "fake-key")
    monkeypatch.setattr(tasks, "_publisher", Mock(return_value=Mock()))
    monkeypatch.setattr(tasks, "_publish_items", publish)
    with pytest.raises(RuntimeError, match="Channel publishing failed"):
        tasks.publish_due(settings, conn)

    assert series_store.get_active().part == 2
    history = series_store.history(series)
    assert [
        (entry.series_id, entry.part, entry.text_de, entry.text_ua) for entry in history
    ] == [
        (series.id, 1, "German", "Ukrainian"),
    ]
    assert series_store.posted_repos() == {"series", "plain"}
    assert weekly.list_unrecorded_published() == []
    assert all(
        weekly.get(weeks[name]).recorded_at is None for name in ("failed", "pending")
    )
    recorded = [weekly.get(weeks[name]).recorded_at for name in ("series", "plain")]
    assert all(recorded)

    monkeypatch.setattr(tasks, "_publisher", Mock(return_value=None))
    monkeypatch.setattr(
        WeeklyStore,
        "mark_recorded",
        Mock(side_effect=AssertionError("Duplicate recording")),
    )
    tasks.publish_due(settings, conn)
    assert series_store.history(series) == history
    assert [
        weekly.get(weeks[name]).recorded_at for name in ("series", "plain")
    ] == recorded
    series_store.complete()


def test_complete_only_matching_active_series(conn):
    store = SeriesStore(conn)
    series = store.start("complete", None)
    assert store.complete(series_id=series.id).status == "completed"
    assert store.get_active() is None
    assert store.complete() is None


def test_weekly_lookup_and_interrupted_query(conn):
    weekly = WeeklyStore(conn)
    repo = ItemRepo(conn)
    week = WEEK + timedelta(weeks=10)
    interrupted = weekly.create_drafted(week, "alpha", None, None, {}, "de", "ua")
    waiting = weekly.create_drafted(
        week + timedelta(days=7), "beta", None, None, {}, "de", "ua"
    )
    unrelated = repo.create_item("github")
    weekly.record_outcome(week + timedelta(days=14), "no_activity", None, {})
    repo.transition(interrupted.id, ItemState.DRAFT)

    assert weekly.get_by_item(waiting.id) == weekly.get(week + timedelta(days=7))
    assert weekly.get_by_item(unrelated.id) is None
    assert weekly.list_interrupted() == [weekly.get_by_item(interrupted.id)]
    repo.transition(interrupted.id, ItemState.AWAITING_APPROVAL)


@pytest.mark.parametrize("fail_second", [False, True])
def test_regenerate_atomic_variant_replace(conn, monkeypatch, fail_second):
    weekly = WeeklyStore(conn)
    repo = ItemRepo(conn)
    facts = {"link_allowed": False, "source_details": "Source: alpha"}
    week = WEEK + timedelta(weeks=13 + int(fail_second))
    item = weekly.create_drafted(week, "alpha", None, None, facts, "Old DE", "Old UA")
    repo.transition(item.id, ItemState.DRAFT)
    model = Mock()
    notifier = Mock()
    monkeypatch.setattr(
        tasks, "_generate_weekly", Mock(return_value=("New DE", "New UA"))
    )
    update = repo.update_variant_text

    def update_text(item_id, lang, text, **kwargs):
        if fail_second and lang == "ua":
            assert repo.get_variants(item_id)[0].text == "New DE"
            raise Error("second variant write failed")
        update(item_id, lang, text, **kwargs)

    monkeypatch.setattr(repo, "update_variant_text", update_text)
    settings = Settings("fake-db", "fake-bot", 123, "fake-key")
    tasks._regenerate_weekly(settings, repo, weekly, notifier, model, item.id)
    assert repo.get_item(item.id).state == ItemState.AWAITING_APPROVAL
    assert weekly.list_interrupted() == []
    assert weekly.get_by_item(item.id).facts == facts
    expected = ["Old DE", "Old UA"] if fail_second else ["New DE", "New UA"]
    assert [variant.text for variant in repo.get_variants(item.id)] == expected
    if fail_second:
        notifier.send_alert.assert_called_once_with(
            "Regeneration failed, previous draft kept (storage)"
        )
    else:
        notifier.send_alert.assert_not_called()
    assert [
        variant.text for variant in notifier.send_preview.call_args.args[1]
    ] == expected
