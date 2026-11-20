from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet

from postpilot.config import ImapSettings, LinkedInSettings, Settings
from postpilot.core.models import Item
from postpilot.core.publisher import Publisher
from postpilot.core.states import ItemState
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
    repo.list_approved_item_ids.return_value = []
    repo.has_inbox_update.return_value = False
    monkeypatch.setattr(tasks, "ItemRepo", Mock(return_value=repo))
    return repo


@pytest.fixture
def api(monkeypatch):
    api = Mock()
    monkeypatch.setattr(tasks, "TelegramApi", Mock(return_value=api))
    return api


def test_publish_due_with_no_channels_leaves_repo_untouched(settings, repo, caplog):
    with caplog.at_level("INFO"):
        tasks.publish_due(replace(settings, linkedin=None), None)
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
    store.get.return_value = SimpleNamespace(
        expires_at=now - timedelta(days=1), access_token="test-token"
    )
    manager = Mock()
    manager.complete_renewal.side_effect = lambda _code: setattr(
        store.get.return_value, "expires_at", now + timedelta(days=1)
    )
    monkeypatch.setattr(tasks, "TokenStore", Mock(return_value=store))
    monkeypatch.setattr(tasks, "_managers", lambda *args: {"linkedin": manager})
    channel = Mock()
    monkeypatch.setattr(tasks, "build_channels", lambda *_: [channel])

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


@pytest.mark.parametrize("outcome", ["published", "failed"])
def test_poll_publishes_telegram_approval_after_saving_offset(
    settings, repo, api, monkeypatch, outcome
):
    telegram_id, github_id = uuid4(), uuid4()
    repo.get_item.return_value = SimpleNamespace(
        state=tasks.ItemState.AWAITING_APPROVAL
    )
    repo.list_approved_item_ids.side_effect = lambda *, source=None: (
        [telegram_id] if source == "telegram" else [github_id, telegram_id]
    )
    api.get_updates.return_value = [
        {
            "update_id": 41,
            "callback_query": {
                "from": {"id": settings.telegram_chat_id},
                "id": "button",
                "data": f"approve:{telegram_id}",
            },
        }
    ]

    def publish(item_id):
        assert item_id == telegram_id
        assert repo.bot_state.get_offset() == 42
        repo.transition.assert_called_once_with(
            telegram_id,
            tasks.ItemState.APPROVED,
            expected_state=tasks.ItemState.AWAITING_APPROVAL,
            approved_by=settings.telegram_chat_id,
        )
        return SimpleNamespace(channels={"test": SimpleNamespace(outcome=outcome)})

    publisher = Mock(publish_item=Mock(side_effect=publish))
    monkeypatch.setattr(tasks, "_publisher", Mock(return_value=publisher))
    if outcome == "failed":
        with pytest.raises(RuntimeError, match="Channel publishing failed"):
            tasks.poll_telegram(settings, None)
    else:
        tasks.poll_telegram(settings, None)
    publisher.publish_item.assert_called_once_with(telegram_id)
    repo.list_approved_item_ids.assert_called_once_with(source="telegram")


@pytest.mark.parametrize("key", [None, "test-api-key"])
def test_poll_only_creates_translation_client_with_key(
    settings, repo, api, monkeypatch, key
):
    settings = replace(settings, anthropic_api_key=key)
    repo.apply_pending_edit.return_value = None
    repo.create_inbox_item.return_value = None
    client = Mock()
    client.return_value.complete.return_value = "Mein Text"
    monkeypatch.setattr(tasks, "ClaudeClient", client)
    api.get_updates.return_value = [
        {
            "update_id": 50,
            "message": {
                "from": {"id": settings.telegram_chat_id},
                "chat": {"id": settings.telegram_chat_id},
                "text": "Мій текст",
            },
        }
    ]

    tasks.poll_telegram(settings, None)

    if key is None:
        client.assert_not_called()
        repo.create_inbox_item.assert_not_called()
        api.send_message.assert_called_once_with(
            settings.telegram_chat_id, "Translation is not configured."
        )
    else:
        client.assert_called_once_with(key)
        repo.create_inbox_item.assert_called_once_with(50, "Мій текст", "Mein Text")


@pytest.mark.parametrize("expired", [True, False])
def test_linkedin_provider_and_expiry_guard(settings, repo, monkeypatch, expired):
    now = datetime.now(UTC)
    monkeypatch.setattr(tasks, "_now", lambda: now)
    item = Item(uuid4(), "telegram", ItemState.PUBLISHING, now, now, 1, now)
    repo.get_item.return_value = item
    repo.list_publications.return_value = []
    repo.get_variants.return_value = []
    repo.claim_publication.return_value = None
    store = Mock()
    store.get.return_value = (
        SimpleNamespace(expires_at=now, access_token="expired-token")
        if expired
        else None
    )
    monkeypatch.setattr(tasks, "TokenStore", Mock(return_value=store))

    def reject_http(request):
        pytest.fail("A held publication must not make an HTTP request")

    with httpx.Client(transport=httpx.MockTransport(reject_http)) as client:
        publisher = tasks._publisher(settings, repo, client)
        report = publisher.publish_item(item.id)

    assert {name: result.outcome for name, result in report.channels.items()} == {
        "linkedin": "held",
        "linkedin-ua": "held",
    }
    assert [call.args for call in store.get.call_args_list] == [
        ("linkedin",),
        ("linkedin",),
    ]
    repo.claim_publication.assert_not_called()


@pytest.mark.parametrize("mode", ["empty", "partial", "failed", "clicks-only"])
def test_snapshot_traffic(settings, monkeypatch, caplog, mode):
    from postpilot.config import ShortioSettings
    from postpilot.sources.github import GitHubError, RepoTraffic

    settings = replace(
        settings,
        github_token="mock-traffic-token",
        shortio=ShortioSettings("mock-shortio-token", "example.short.gy"),
    )
    now = datetime(2026, 11, 19, tzinfo=UTC)
    monkeypatch.setattr(tasks, "_now", lambda: now)
    store = Mock()
    repos = {"bad", "good"} if mode in ("partial", "failed") else set()
    store.list_links.return_value = (
        []
        if mode == "empty"
        else [
            SimpleNamespace(link_id="unavailable"),
            SimpleNamespace(link_id="available"),
        ]
    )
    monkeypatch.setattr(tasks, "LinkStore", Mock(return_value=store))
    monkeypatch.setattr(
        tasks,
        "SeriesStore",
        Mock(return_value=Mock(posted_repos=Mock(return_value=repos))),
    )
    traffic = RepoTraffic(3, 2, 1, 1, {"github.com": 2})
    github = Mock()
    github.traffic.side_effect = [
        GitHubError("mock-traffic-token"),
        traffic if mode == "partial" else None,
    ]
    monkeypatch.setattr(tasks, "GitHubClient", Mock(return_value=github))
    shortio = Mock()
    shortio.total_clicks.side_effect = [None, None if mode == "failed" else 0]
    monkeypatch.setattr(tasks, "ShortioClient", Mock(return_value=shortio))

    if mode == "failed":
        with pytest.raises(
            RuntimeError, match="All traffic and click snapshots failed"
        ):
            tasks.snapshot_traffic(settings, Mock())
    else:
        tasks.snapshot_traffic(settings, Mock())

    if mode == "partial":
        store.upsert_traffic.assert_called_once_with("good", now.date(), traffic)
    else:
        store.upsert_traffic.assert_not_called()
    if mode in ("partial", "clicks-only"):
        store.upsert_clicks.assert_called_once_with("available", now.date(), 0)
    else:
        store.upsert_clicks.assert_not_called()
    if mode == "empty":
        tasks.GitHubClient.assert_not_called()
        tasks.ShortioClient.assert_not_called()
        assert not caplog.records
    else:
        shortio.close.assert_called_once()
        assert "unavailable" in caplog.text
    assert "mock-traffic-token" not in caplog.text


@pytest.mark.parametrize("expired", [False, True])
def test_poll_engagement_no_sources_is_noop(settings, monkeypatch, expired):
    store = Mock()
    store.get.return_value = (
        SimpleNamespace(
            access_token="stored", expires_at=datetime.now(UTC) - timedelta(days=1)
        )
        if expired
        else None
    )
    monkeypatch.setattr(tasks, "TokenStore", Mock(return_value=store))
    poll = Mock()
    monkeypatch.setattr(tasks, "poll_engagement_sources", poll)
    tasks.poll_engagement(settings, Mock())
    store.get.assert_called_once_with("threads")
    poll.assert_not_called()


def test_poll_engagement_runs_threads_with_stored_token_without_imap(
    settings, monkeypatch
):
    store = Mock()
    store.get.return_value = SimpleNamespace(
        access_token="stored", expires_at=datetime.now(UTC) + timedelta(days=1)
    )
    monkeypatch.setattr(tasks, "TokenStore", Mock(return_value=store))
    collect = Mock()
    monkeypatch.setattr(tasks, "collect_threads", collect)
    monkeypatch.setattr(tasks, "ClaudeClient", Mock())
    engagement = Mock()
    engagement.list_unnotified.return_value = []
    monkeypatch.setattr(tasks, "EngagementStore", Mock(return_value=engagement))
    conn = Mock()
    tasks.poll_engagement(replace(settings, anthropic_api_key="fixture"), conn)
    store.get.assert_called_once_with("threads")
    collect.assert_called_once()


def test_poll_engagement_isolates_threads_token_lookup_failure(settings, monkeypatch):
    monkeypatch.setattr(
        tasks,
        "_read_token",
        Mock(side_effect=RuntimeError("private token store error")),
    )
    linkedin = Mock()
    monkeypatch.setattr(tasks, "collect_linkedin", linkedin)
    monkeypatch.setattr(tasks, "ClaudeClient", Mock())
    engagement = Mock()
    engagement.list_unnotified.return_value = []
    monkeypatch.setattr(tasks, "EngagementStore", Mock(return_value=engagement))
    conn = Mock()
    configured = replace(
        settings,
        imap=ImapSettings("mailbox@example.test", "fixture"),
        anthropic_api_key="fixture",
    )

    with pytest.raises(
        RuntimeError, match="^Engagement source or notification delivery failed$"
    ):
        tasks.poll_engagement(configured, conn)

    linkedin.assert_called_once()
    engagement.list_unnotified.assert_called_once_with()
