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

    migrate.down(conn)
    assert "tiktok_settings" not in test_store.table_names(conn)
    migrate.up(conn)
    assert store.get(item_id) is None
    store.save(item_id, changed)
    assert store.get(item_id) == changed
    with conn.transaction():
        conn.execute("DELETE FROM items WHERE id = %s", (item_id,))
    assert store.get(item_id) is None
