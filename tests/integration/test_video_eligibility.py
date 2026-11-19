from datetime import UTC, datetime, timedelta

import test_store

from postpilot.core.states import ItemState
from postpilot.store.repo import ItemRepo
from postpilot.video.store import VideoStore

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn


def test_oldest_eligible_german_variant_and_explicit_retry(conn):
    repo = ItemRepo(conn)
    store = VideoStore(conn)

    def add(offset, state, lang):
        item = repo.create_item("github")
        repo.add_variant(item.id, lang, f"Post {offset}")
        with conn.transaction():
            conn.execute(
                "UPDATE items SET state = %s, created_at = %s WHERE id = %s",
                (
                    state,
                    datetime(2026, 11, 1, tzinfo=UTC) + timedelta(days=offset),
                    item.id,
                ),
            )
        return item.id

    add(0, ItemState.AWAITING_APPROVAL, "de")
    add(1, ItemState.APPROVED, "ua")
    failed = add(2, ItemState.APPROVED, "de")
    failed_render = store.claim(failed, "de")
    assert store.mark_failed(failed_render)
    stuck = add(3, ItemState.APPROVED, "de")
    store.claim(stuck, "de")
    oldest = add(4, ItemState.PUBLISHED, "de")
    store.claim(oldest, "ua")
    newer = add(5, ItemState.APPROVED, "de")

    assert repo.next_video_variant().item_id == oldest
    assert repo.next_video_variant(retry=True).item_id == failed
    store.claim(oldest, "de")
    assert repo.next_video_variant().item_id == newer
    store.claim(newer, "de")
    publishing = add(6, ItemState.PUBLISHING, "de")
    assert repo.next_video_variant().item_id == publishing
    store.claim(publishing, "de")
    assert repo.next_video_variant() is None

    retry_id = store.claim(failed, "de", retry=True)
    assert store.mark_failed(retry_id)
    assert repo.next_video_variant(retry=True) is None
