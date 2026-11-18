from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from postpilot.config import LinkedInSettings, Settings
from postpilot.core.publisher import Publisher
from postpilot.jobs import tasks
from postpilot.tokens.oauth_state import make_state


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


class State:
    def __init__(self):
        self.offset = 0
        self.sent = set()
        self.nonces = set()

    def get_offset(self):
        return self.offset

    def set_offset(self, offset):
        self.offset = offset

    def send_token_alert_once(self, provider, day, send):
        self._send_once(("alert", provider, day), send)

    def send_renewal_prompt_once(self, provider, day, send):
        self._send_once(("renewal", provider, day), send)

    def _send_once(self, key, send):
        if key not in self.sent:
            send()
            self.sent.add(key)

    def save_oauth_nonce(self, nonce):
        self.nonces.add(nonce)

    def consume_oauth_nonce(self, nonce):
        if nonce not in self.nonces:
            return False
        self.nonces.remove(nonce)
        return True


@pytest.fixture
def repo(monkeypatch):
    repo = Mock(bot_state=State())
    monkeypatch.setattr(tasks, "ItemRepo", Mock(return_value=repo))
    return repo


@pytest.fixture
def api(monkeypatch):
    api = Mock()
    monkeypatch.setattr(tasks, "TelegramApi", Mock(return_value=api))
    return api


def test_publish_due_with_no_channels_leaves_repo_untouched(settings, repo, caplog):
    with caplog.at_level("INFO"):
        tasks.publish_due(settings, None)
    assert "no channels configured" in caplog.text
    assert repo.mock_calls == []


def test_publisher_rejects_no_channels_before_reading_item(repo):
    with pytest.raises(ValueError, match="No channels configured"):
        Publisher(repo, []).publish_item(uuid4())
    assert repo.mock_calls == []


def test_poll_telegram_advances_offset(settings, repo, api):
    api.get_updates.return_value = [{"update_id": 41}]
    tasks.poll_telegram(settings, None)
    api.get_updates.assert_called_once_with(0)
    assert repo.bot_state.get_offset() == 42


def test_check_tokens_deduplicates_daily_alert_and_renewal(
    settings, repo, api, monkeypatch
):
    now = datetime(2025, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(tasks, "_now", lambda: now)
    manager = Mock(provider="linkedin", days_left=Mock(return_value=None))
    manager.renewal_url.return_value = "https://renew.example.test"
    monkeypatch.setattr(tasks, "_managers", lambda *args: {"linkedin": manager})
    tasks.check_tokens(settings, None)
    tasks.check_tokens(settings, None)
    assert api.send_message.call_count == 2
    assert "token missing" in api.send_message.call_args_list[0].args[1]
    assert "Renew LinkedIn token" in api.send_message.call_args_list[1].args[1]
    now += timedelta(days=1)
    tasks.check_tokens(settings, None)
    assert api.send_message.call_count == 4


def test_renewal_releases_held_items_with_current_token(
    settings, repo, api, monkeypatch
):
    now = datetime.now(UTC)
    item_id = uuid4()
    repo.list_unfinished_item_ids.return_value = [item_id]
    store = Mock()
    store.get.return_value = SimpleNamespace(expires_at=now - timedelta(days=1))
    manager = Mock()
    manager.complete_renewal.side_effect = lambda _code: setattr(
        store.get.return_value, "expires_at", now + timedelta(days=1)
    )
    monkeypatch.setattr(tasks, "TokenStore", Mock(return_value=store))
    monkeypatch.setattr(tasks, "_managers", lambda *args: {"linkedin": manager})
    channel = Mock()
    monkeypatch.setattr(tasks, "build_channels", lambda _: [channel])

    publisher = Mock()
    publisher.publish_item.return_value.channels = {}
    build_publisher = Mock(return_value=publisher)
    monkeypatch.setattr(tasks, "Publisher", build_publisher)

    state = make_state(
        settings.token_encryption_key.encode(), "linkedin", now, store=repo.bot_state
    )
    api.get_updates.return_value = [
        {
            "update_id": 19,
            "message": {
                "message_id": 2,
                "from": {"id": settings.telegram_chat_id},
                "chat": {"id": settings.telegram_chat_id},
                "text": f"/renew test-code {state}",
            },
        }
    ]
    tasks.poll_telegram(settings, None)
    manager.complete_renewal.assert_called_once_with("test-code")
    publisher.publish_item.assert_called_once_with(item_id)
    guard = build_publisher.call_args.kwargs["token_guard"]
    assert guard("linkedin")
    store.get.return_value.expires_at = now
    assert not guard("linkedin")
    store.get.return_value = None
    assert not guard("linkedin")
    assert repo.bot_state.get_offset() == 20


def test_publish_due_processes_unfinished_and_approved_items(
    settings, repo, monkeypatch
):
    unfinished_id, approved_id = uuid4(), uuid4()
    repo.list_unfinished_item_ids.return_value = [unfinished_id]
    repo.list_approved_item_ids.return_value = [approved_id]
    publisher = Mock()
    publisher.publish_item.return_value.channels = {}
    monkeypatch.setattr(tasks, "_publisher", Mock(return_value=publisher))

    tasks.publish_due(settings, None)

    assert [call.args for call in publisher.publish_item.call_args_list] == [
        (unfinished_id,),
        (approved_id,),
    ]
