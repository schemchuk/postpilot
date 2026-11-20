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
