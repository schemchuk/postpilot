from datetime import UTC, datetime, timedelta
from uuid import uuid4

import test_store

from postpilot.core.tiktok import TikTokChoice
from postpilot.store import migrate
from postpilot.store.tiktok_settings import TikTokSettingsStore

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn


def test_tiktok_settings_migration_and_store(conn):
    item_id = uuid4()
    with conn.transaction():
        conn.execute(
            "INSERT INTO items (id, source, state) VALUES (%s, 'own_video', 'DRAFT')",
            (item_id,),
        )
        conn.execute(
            "INSERT INTO tiktok_settings (item_id, privacy_level) VALUES (%s, 'SELF_ONLY')",
            (item_id,),
        )
    store = TikTokSettingsStore(conn)
    assert store.get(item_id) == TikTokChoice("SELF_ONLY")
    changed = TikTokChoice("PUBLIC_TO_EVERYONE", allow_comment=True, allow_stitch=True)
    store.save(item_id, changed)
    assert store.get(item_id) == changed
    now = datetime.now(UTC)
    assert store.get(item_id, now - timedelta(days=1)) is None
    assert store.get(item_id, now + timedelta(days=1)) == changed

    migrate.down(conn, 2)
    assert "tiktok_settings" not in test_store.table_names(conn)
    migrate.up(conn)
    assert store.get(item_id) is None
    store.save(item_id, changed)
    assert store.get(item_id) == changed
    with conn.transaction():
        conn.execute("DELETE FROM items WHERE id = %s", (item_id,))
    assert store.get(item_id) is None


def test_partial_choice_migration_preserves_complete_choices(conn):
    import pytest
    from psycopg.errors import NotNullViolation

    from postpilot.core.tiktok import TikTokSelection
    from postpilot.store.repo import ItemRepo

    repo = ItemRepo(conn)
    chosen = repo.create_item("own_video").id
    partial = repo.create_item("own_video").id
    empty = repo.create_item("own_video").id
    store = TikTokSettingsStore(conn)
    now = datetime.now(UTC)

    store.flip_toggle(chosen, "comment", now)
    store.flip_toggle(chosen, "duet", now)
    store.flip_toggle(chosen, "duet", now)
    assert store.get(chosen) is None
    assert store.get_partial(chosen) == TikTokSelection(allow_comment=True)
    store.set_privacy(chosen, "SELF_ONLY", now)
    assert store.get(chosen, by=now) == TikTokChoice("SELF_ONLY", allow_comment=True)
    assert store.get(chosen, by=now - timedelta(microseconds=1)) is None
    store.flip_toggle(partial, "stitch", now)
    assert store.get_partial(partial) == TikTokSelection(allow_stitch=True)

    migrate.down(conn)
    with pytest.raises(NotNullViolation):
        store.flip_toggle(empty, "comment", now)
    assert store.get(chosen) == TikTokChoice("SELF_ONLY", allow_comment=True)
    migrate.up(conn)
    assert store.get_partial(partial) == TikTokSelection()
    assert store.get(chosen) == TikTokChoice("SELF_ONLY", allow_comment=True)


def test_channel_skip_survives_retries_and_allows_completion(conn):
    from postpilot.core.models import PublicationStatus
    from postpilot.core.states import ItemState
    from postpilot.store.repo import ItemRepo

    repo = ItemRepo(conn)
    item = repo.create_item("own_video")
    repo.transition(item.id, ItemState.AWAITING_APPROVAL)
    repo.transition(item.id, ItemState.APPROVED, approved_by=1)
    repo.transition(item.id, ItemState.PUBLISHING)
    claim = repo.claim_publication(item.id, "tiktok")
    repo.mark_skipped(claim, "no TikTok privacy choice")
    assert repo.claim_publication(item.id, "tiktok") is None
    (publication,) = repo.list_publications(item.id)
    assert publication.status is PublicationStatus.SKIPPED
    assert publication.error == "no TikTok privacy choice"
    repo.finish_publishing(item.id, {"tiktok", "facebook"})
    assert repo.get_item(item.id).state is ItemState.PUBLISHING
    claim = repo.claim_publication(item.id, "facebook")
    repo.mark_published(item.id, "facebook", "remote", claim=claim)
    repo.finish_publishing(item.id, {"tiktok", "facebook"})
    assert repo.get_item(item.id).state is ItemState.PUBLISHED
