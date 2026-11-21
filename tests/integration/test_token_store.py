from datetime import UTC, datetime, timedelta

import test_store
from cryptography.fernet import Fernet

from postpilot.store import migrate
from postpilot.store.bot_state import BotState
from postpilot.tokens.crypto import TokenCipher
from postpilot.tokens.store import StoredToken, TokenStore

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn


def test_token_store_encrypts_and_upserts(conn):
    cipher = TokenCipher(Fernet.generate_key())
    store = TokenStore(conn, cipher)
    expiry = datetime(2026, 11, 18, tzinfo=UTC)
    access, refresh = "example access value", "example refresh value"
    assert store.get("provider") is None
    store.save("provider", access, expiry, refresh)
    with conn.transaction():
        raw_access, raw_refresh = conn.execute(
            "SELECT access_token, refresh_token FROM tokens WHERE provider = 'provider'"
        ).fetchone()
    assert raw_access != access
    assert raw_refresh != refresh
    assert cipher.decrypt(raw_access) == access
    assert cipher.decrypt(raw_refresh) == refresh
    stored = store.get("provider")
    assert stored == StoredToken("provider", access, expiry, refresh)
    assert access not in repr(stored)
    assert refresh not in repr(stored)

    updated_expiry = expiry + timedelta(days=30)
    store.save("provider", "updated access value", updated_expiry)
    assert store.get("provider") == StoredToken(
        "provider", "updated access value", updated_expiry
    )
    store.delete("provider")
    assert store.get("provider") is None


def test_token_migration_round_trip(conn):
    assert "tokens" in test_store.table_names(conn)
    migrate.down(conn)  # 0015_series_queue
    migrate.down(conn)  # 0014_series_planned_parts
    migrate.down(conn)  # 0013_weekly_drafts
    migrate.down(conn)  # 0012_engagement_replies
    migrate.down(conn)  # 0011_github_engagement
    migrate.down(conn)  # 0010_engagement_events
    migrate.down(conn)  # 0009_video_publications
    assert "video_publications" not in test_store.table_names(conn)
    migrate.down(conn)  # 0008_publication_remote_ref
    migrate.down(conn)  # 0007_video_renders
    migrate.down(conn)  # 0006_link_tracking
    migrate.down(conn)  # 0005_inbox_updates
    migrate.down(conn)  # 0004_series
    migrate.down(conn)  # 0003_tokens
    assert "tokens" not in test_store.table_names(conn)
    assert "bot_state" in test_store.table_names(conn)
    migrate.up(conn)
    assert "tokens" in test_store.table_names(conn)


def test_token_alert_daily_state(conn):
    state = BotState(conn)
    today = datetime(2026, 11, 18, tzinfo=UTC).date()
    messages = []
    state.send_token_alert_once("one", today, lambda: messages.append("first"))
    state.send_token_alert_once("one", today, lambda: messages.append("duplicate"))
    tomorrow = today + timedelta(days=1)
    state.send_token_alert_once("one", tomorrow, lambda: messages.append("next day"))
    with conn.transaction():
        row = conn.execute(
            "SELECT value FROM bot_state WHERE key = 'token_alert:one'"
        ).fetchone()
    assert row == (tomorrow.isoformat(),)
    assert messages == ["first", "next day"]
