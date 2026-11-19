from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
import test_store
from cryptography.fernet import Fernet

from postpilot.channels.fake import FakeChannel
from postpilot.config import LinkedInSettings, Settings
from postpilot.core.states import ItemState
from postpilot.jobs import tasks
from postpilot.tokens.crypto import TokenCipher
from postpilot.tokens.oauth_state import make_state
from postpilot.tokens.store import TokenStore

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn
repo = test_store.repo
approved_item = test_store.approved_item


@pytest.fixture(autouse=True)
def reset_job_state(conn):
    with conn.transaction():
        conn.execute(
            "TRUNCATE items, variants, publications, pending_edits, bot_state, tokens, inbox_updates"
        )


@pytest.fixture
def settings():
    return Settings(
        "fake-db",
        "fake-token",
        123,
        Fernet.generate_key().decode(),
        LinkedInSettings(
            "test-client", "test-secret", "https://callback.example.test/", ("openid",)
        ),
    )


@pytest.fixture
def api(monkeypatch):
    api = Mock()
    monkeypatch.setattr(tasks, "TelegramApi", Mock(return_value=api))
    return api


def test_empty_channels_leave_items_untouched(conn, repo, approved_item, settings):
    tasks.publish_due(settings, conn)
    assert repo.get_item(approved_item.id).state == ItemState.APPROVED
    assert repo.list_publications(approved_item.id) == []


def test_poll_saves_offset(conn, repo, settings, api):
    update_id = repo.bot_state.get_offset() + 10
    api.get_updates.return_value = [{"update_id": update_id}]
    tasks.poll_telegram(settings, conn)
    assert repo.bot_state.get_offset() == update_id + 1


def test_check_tokens_deduplicates_prompts_in_postgres(
    conn, settings, api, monkeypatch
):
    monkeypatch.setattr(tasks, "_now", lambda: datetime(2025, 1, 1, tzinfo=UTC))
    tasks.check_tokens(settings, conn)
    tasks.check_tokens(settings, conn)
    assert api.send_message.call_count == 2
    assert "token missing" in api.send_message.call_args_list[0].args[1]
    assert "Renew LinkedIn token" in api.send_message.call_args_list[1].args[1]


def test_renew_releases_held_item(
    conn, repo, approved_item, settings, api, monkeypatch
):
    now = datetime.now(UTC)
    store = TokenStore(conn, TokenCipher(settings.token_encryption_key))
    store.save("linkedin", "test-expired-token", now - timedelta(days=1))
    repo.add_variant(approved_item.id, "de", "Ein Beitrag")
    channel = FakeChannel("linkedin")
    monkeypatch.setattr(tasks, "build_channels", lambda _: [channel])
    tasks.publish_due(settings, conn)
    assert repo.get_item(approved_item.id).state == ItemState.PUBLISHING
    assert channel.posts == {}

    expiry = now + timedelta(days=10)

    def renew(_code):
        store.save("linkedin", "test-renewed-token", expiry)
        return expiry

    manager = Mock(complete_renewal=Mock(side_effect=renew))
    monkeypatch.setattr(tasks, "_managers", lambda *args: {"linkedin": manager})
    state = make_state(
        settings.token_encryption_key.encode(), "linkedin", now, store=repo.bot_state
    )
    api.get_updates.return_value = [
        {
            "update_id": repo.bot_state.get_offset() + 10,
            "message": {
                "message_id": 2,
                "from": {"id": settings.telegram_chat_id},
                "chat": {"id": settings.telegram_chat_id},
                "text": f"/renew test-code {state}",
            },
        }
    ]
    tasks.poll_telegram(settings, conn)
    assert repo.get_item(approved_item.id).state == ItemState.PUBLISHED
    assert len(channel.posts) == 1
    tasks.publish_due(settings, conn)
    assert len(channel.posts) == 1


def test_poll_approval_publishes_telegram_and_leaves_github_for_friday(
    conn, repo, approved_item, settings, api, monkeypatch
):
    telegram = repo.create_inbox_item(101, "Мій текст", "Mein Text")
    api.get_updates.return_value = [
        {
            "update_id": 102,
            "callback_query": {
                "from": {"id": settings.telegram_chat_id},
                "id": "approve-button",
                "data": f"approve:{telegram.id}",
            },
        }
    ]
    channel = FakeChannel("linkedin")
    monkeypatch.setattr(tasks, "build_channels", lambda _: [channel])
    TokenStore(conn, TokenCipher(settings.token_encryption_key)).save(
        "linkedin", "test-token", datetime.now(UTC) + timedelta(days=10)
    )

    tasks.poll_telegram(settings, conn)

    assert repo.get_item(telegram.id).state == ItemState.PUBLISHED
    assert repo.get_item(approved_item.id).state == ItemState.APPROVED
    assert len(channel.posts) == 1
    assert repo.bot_state.get_offset() == 103
