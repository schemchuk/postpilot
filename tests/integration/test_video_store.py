from contextlib import ExitStack
from datetime import UTC, datetime, timedelta, timezone

import test_store

from postpilot.store.repo import ItemRepo
from postpilot.video.store import VideoStatus, VideoStore

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn


def test_video_render_lifecycle_and_utc_month(conn, database_schema):
    repo = ItemRepo(conn)
    item = repo.create_item("github")
    store = VideoStore(conn)
    ready_id = store.claim(item.id, "de")
    failed_id = store.claim(item.id, "ua")
    old_id = store.claim(repo.create_item("github").id, "de")
    rendering_id = store.claim(repo.create_item("github").id, "de")
    assert all((ready_id, failed_id, old_id, rendering_id))
    assert store.claim(item.id, "de") is None

    november = datetime(2026, 11, 1, tzinfo=UTC)
    december = datetime(2026, 12, 1, tzinfo=UTC)
    with conn.transaction():
        conn.execute("UPDATE video_renders SET requested_at = %s", (november,))
        conn.execute(
            "UPDATE video_renders SET requested_at = %s WHERE id = %s",
            (november - timedelta(microseconds=1), old_id),
        )
        conn.execute(
            "UPDATE video_renders SET requested_at = %s WHERE id = %s",
            (december, rendering_id),
        )
    assert store.count_this_month(november) == 2
    assert store.count_this_month(december) == 1
    assert (
        store.count_this_month(december.astimezone(timezone(timedelta(hours=-5)))) == 1
    )

    assert store.mark_failed(failed_id)
    assert store.count_this_month(november) == 1
    assert not store.approve(failed_id, 123, november)
    assert store.claim(item.id, "ua") is None
    retry_id = store.claim(item.id, "ua", retry=True)
    assert retry_id is not None and retry_id != failed_id
    assert not store.attach(failed_id, "stale", "script", b"stale", 30.0)
    assert store.mark_failed(retry_id)
    assert store.claim(item.id, "ua", retry=True) is None

    assert not store.approve(ready_id, 123, november)
    assert store.attach(ready_id, "remote-id", "Guten Morgen!", b"labelled mp4", 24.5)
    assert not store.mark_failed(ready_id)
    assert store.get(ready_id).status is VideoStatus.READY
    assert store.approve(ready_id, 123, november)
    assert not store.reject(ready_id, 456)
    assert store.claim(item.id, "de", retry=True) is None

    assert store.attach(rendering_id, "other", "script", b"labelled mp4", None)
    assert store.reject(rendering_id, 123)
    assert not store.approve(rendering_id, 123, november)

    with test_store.connect(test_store.DATABASE_URL, schema=database_schema) as fresh:
        persisted = VideoStore(fresh)
        approved = persisted.get(ready_id)
        assert approved.status is VideoStatus.APPROVED
        assert approved.heygen_video_id == "remote-id"
        assert approved.script == "Guten Morgen!"
        assert approved.video == b"labelled mp4"
        assert approved.duration == 24.5
        assert approved.approved_by == 123
        assert approved.approved_at == november
        assert not persisted.approve(ready_id, 456, november)
        assert persisted.get(rendering_id).status is VideoStatus.REJECTED
        assert persisted.get(failed_id) is None


def test_render_lease_excludes_other_connections_and_reclaims_expired_owner(
    conn, database_schema
):
    store = VideoStore(conn)
    with test_store.connect(test_store.DATABASE_URL, schema=database_schema) as fresh:
        other = VideoStore(fresh)
        with store.render_lock() as acquired:
            assert acquired
            with other.render_lock() as concurrent:
                assert not concurrent
        with ExitStack() as replacement:
            with store.render_lock() as acquired:
                assert acquired
                with fresh.transaction():
                    fresh.execute(
                        "UPDATE bot_state SET value = '2000-01-01T00:00:00Z' "
                        "WHERE key = 'video_render_lease'"
                    )
                assert replacement.enter_context(other.render_lock())
            # The expired owner's cleanup must leave the replacement in place.
            with store.render_lock() as concurrent:
                assert not concurrent
        with other.render_lock() as acquired:
            assert acquired
