from datetime import UTC, datetime, timedelta

import test_store

from postpilot.engagement.store import EngagementStore
from postpilot.store.bot_state import BotState
from postpilot.store.repo import ItemRepo

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn


def test_engagement_store_round_trip(conn, database_schema):
    now = datetime.now(UTC)
    state = BotState(conn)
    assert state.get_imap_cursor() is None
    store = EngagementStore(conn)
    args = ("linkedin", "comment", "imap:10:15", "Actor", "Comment", None, "Draft", now)
    event = store.insert_if_new(*args)
    assert event is not None
    assert store.insert_if_new(*args) is None
    assert store.get(event.id) == event
    assert store.list_unnotified() == [event]
    state.set_imap_cursor(10, 15)
    store.mark_notified(event.id, 2**40)
    store.set_reply_draft(event.id, "New draft")

    with test_store.connect(test_store.DATABASE_URL, schema=database_schema) as fresh:
        stored = EngagementStore(fresh)
        assert stored.list_unnotified() == []
        updated = stored.get(event.id)
        assert updated.notified_at is not None
        assert updated.telegram_message_id == 2**40
        assert updated.reply_draft == "New draft"
        assert BotState(fresh).get_imap_cursor() == (10, 15)


def test_recent_threads_posts_filters_channel_status_age_and_missing_id(conn):
    repo = ItemRepo(conn)
    since = datetime.now(UTC) - timedelta(days=14)
    for channel, status, remote_id, updated_at in (
        ("threads", "PUBLISHED", "recent", since),
        ("threads", "PUBLISHED", "old", since - timedelta(seconds=1)),
        ("threads", "PUBLISHED", None, since),
        ("threads", "FAILED", "failed", since),
        ("linkedin", "PUBLISHED", "linkedin", since),
    ):
        item = repo.create_item("github")
        with conn.transaction():
            conn.execute(
                "INSERT INTO publications (item_id, channel, status, remote_id, updated_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (item.id, channel, status, remote_id, updated_at),
            )
    assert EngagementStore(conn).recent_threads_posts(since) == ["recent"]


def test_known_refs_filters_channel_and_requested_refs(conn):
    store = EngagementStore(conn)
    now = datetime.now(UTC)
    for channel, ref in (
        ("threads", "known"),
        ("threads", "unrequested"),
        ("linkedin", "other-channel"),
    ):
        store.insert_if_new(channel, "comment", ref, None, None, None, None, now)
    assert store.known_refs(
        "threads", ["known", "unknown", "other-channel", "known"]
    ) == {"known"}
    assert store.known_refs("threads", []) == set()


def test_purge_clears_personal_data_and_filters_old_unnotified(conn, monkeypatch):
    from postpilot.engagement import store as engagement_store

    now = datetime.now(UTC)
    cutoff = now - timedelta(days=90)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(engagement_store, "datetime", Clock)
    store = EngagementStore(conn)
    old = store.insert_if_new(
        "linkedin",
        "comment",
        "old",
        "Actor",
        "Text",
        "https://example.test",
        "Draft",
        cutoff - timedelta(microseconds=1),
    )
    partial = store.insert_if_new(
        "linkedin",
        "comment",
        "partial",
        None,
        None,
        None,
        "Draft",
        cutoff - timedelta(days=2),
    )
    edge = store.insert_if_new(
        "linkedin",
        "comment",
        "edge",
        "Actor",
        "Text",
        None,
        "Draft",
        cutoff,
    )
    assert [
        e for e in store.list_unnotified() if e.id in {old.id, partial.id, edge.id}
    ] == [edge]
    assert store.purge_personal_data(now) == 2
    assert store.purge_personal_data(now) == 0
    assert [
        e for e in store.list_unnotified() if e.id in {old.id, partial.id, edge.id}
    ] == [edge]
    assert store.get(edge.id) == edge
    for event in (old, partial):
        retained = store.get(event.id)
        assert (retained.actor, retained.text, retained.reply_draft) == (
            None,
            None,
            None,
        )
        assert retained.post_url == event.post_url
        assert retained.kind == event.kind


def test_github_watch_and_engagement_round_trip(conn, database_schema):
    state = BotState(conn)
    store = EngagementStore(conn)
    now = datetime.now(UTC)
    assert state.get_github_watch() == {}
    state.set_github_watch("123", {"stars": 1, "forks": 0})
    state.set_github_watch("456", {"stars": 0, "forks": 0})
    state.set_github_watch("123", {"stars": 1, "forks": 1})

    star_args = ("github", "star", "star:123:10", "star-user", None, None, None, now)
    fork_args = ("github", "fork", "fork:123:20", "fork-user", None, None, None, now)
    star = store.insert_if_new(*star_args, notified=True)
    fork = store.insert_if_new(*fork_args)
    assert star is not None and star.notified_at is not None
    assert fork is not None and fork.notified_at is None
    assert store.insert_if_new(*star_args) is None
    assert store.insert_if_new(*fork_args, notified=True) is None

    with test_store.connect(test_store.DATABASE_URL, schema=database_schema) as fresh:
        assert [
            event
            for event in EngagementStore(fresh).list_unnotified()
            if event.id in {star.id, fork.id}
        ] == [fork]
        assert BotState(fresh).get_github_watch() == {
            "123": {"stars": 1, "forks": 1},
            "456": {"stars": 0, "forks": 0},
        }
