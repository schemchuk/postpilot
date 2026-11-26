from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, call
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet

from postpilot.config import ImapSettings, LinkedInSettings, Settings
from postpilot.core.models import Item, PublicationStatus
from postpilot.core.publisher import ChannelResult, Publisher, PublishReport
from postpilot.core.states import ItemState
from postpilot.jobs import tasks
from postpilot.notify.telegram import Notifier, TelegramError
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
        self.sent = set()
        self.nonces = set()

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
    monkeypatch.setattr(
        tasks,
        "WeeklyStore",
        Mock(return_value=Mock(list_interrupted=Mock(return_value=[]))),
    )
    repo.list_approved_item_ids.return_value = []
    repo.list_unfinished_item_ids.return_value = []
    repo.has_inbox_update.return_value = False
    monkeypatch.setattr(tasks, "ItemRepo", Mock(return_value=repo))
    return repo


@pytest.fixture
def api(monkeypatch):
    api = Mock()
    monkeypatch.setattr(tasks, "TelegramApi", Mock(return_value=api))
    return api


@pytest.fixture
def inbox(monkeypatch):
    inbox = Mock()
    monkeypatch.setattr(tasks, "UpdateInbox", Mock(return_value=inbox))
    return inbox


@pytest.fixture
def record_weekly(monkeypatch):
    record = Mock()
    monkeypatch.setattr(tasks, "_record_weekly_publications", record)
    return record


def test_publish_due_with_no_channels_leaves_repo_untouched(
    settings, repo, caplog, record_weekly
):
    with caplog.at_level("INFO"):
        tasks.publish_due(replace(settings, linkedin=None), None)
    record_weekly.assert_called_once_with(None, repo)
    assert "no channels configured" in caplog.text
    assert repo.mock_calls == []


def test_publisher_rejects_no_channels_before_reading_item(repo):
    with pytest.raises(ValueError, match="No channels configured"):
        Publisher(repo, []).publish_item(uuid4())
    assert repo.mock_calls == []


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
    settings, repo, inbox, api, monkeypatch
):
    now = datetime.now(UTC)
    item_id = uuid4()
    repo.list_unfinished_item_ids.side_effect = lambda *, source=None: (
        [item_id] if source is None else []
    )
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
    publisher.publish_item.return_value = PublishReport({})
    build_publisher = Mock(return_value=publisher)
    monkeypatch.setattr(tasks, "Publisher", build_publisher)

    state = make_state(
        settings.token_encryption_key.encode(), "linkedin", now, store=repo.bot_state
    )
    updates = [
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
    inbox.pending.return_value = [(update["update_id"], update) for update in updates]
    tasks.poll_telegram(settings, None)
    manager.complete_renewal.assert_called_once_with("test-code")
    publisher.publish_item.assert_called_once_with(item_id)
    guard = build_publisher.call_args.kwargs["token_guard"]
    assert guard("linkedin")
    store.get.return_value.expires_at = now
    assert not guard("linkedin")
    store.get.return_value = None
    assert not guard("linkedin")
    inbox.mark_processed.assert_called_once_with(19)


def test_publish_due_processes_unfinished_and_approved_items(
    settings, repo, monkeypatch, record_weekly
):
    unfinished_id, approved_id = uuid4(), uuid4()
    repo.list_unfinished_item_ids.return_value = [unfinished_id]
    repo.list_approved_item_ids.return_value = [approved_id]
    publisher = Mock()
    publisher.publish_item.return_value = PublishReport({})
    monkeypatch.setattr(tasks, "_publisher", Mock(return_value=publisher))

    tasks.publish_due(settings, None)

    assert [call.args for call in publisher.publish_item.call_args_list] == [
        (unfinished_id,),
        (approved_id,),
    ]


@pytest.mark.parametrize("source", ["telegram", "own_video"])
@pytest.mark.parametrize("outcome", ["published", "failed"])
def test_poll_publishes_immediate_approval_after_marking_update_processed(
    settings, repo, inbox, api, monkeypatch, outcome, source
):
    approved_id, github_id = uuid4(), uuid4()
    repo.get_item.return_value = SimpleNamespace(
        state=tasks.ItemState.AWAITING_APPROVAL
    )

    def list_approved(*, source=None):
        if source is None:
            return [github_id, approved_id]
        return [approved_id] if source == approved_source else []

    approved_source = source
    repo.list_approved_item_ids.side_effect = list_approved
    updates = [
        {
            "update_id": 41,
            "callback_query": {
                "from": {"id": settings.telegram_chat_id},
                "id": "button",
                "data": f"approve:{approved_id}",
            },
        }
    ]

    inbox.pending.return_value = [(update["update_id"], update) for update in updates]

    def publish(item_id):
        assert item_id == approved_id
        inbox.mark_processed.assert_called_once_with(41)
        repo.transition.assert_called_once_with(
            approved_id,
            tasks.ItemState.APPROVED,
            expected_state=tasks.ItemState.AWAITING_APPROVAL,
            approved_by=settings.telegram_chat_id,
        )
        return PublishReport({"test": ChannelResult(outcome)})

    publisher = Mock(publish_item=Mock(side_effect=publish))
    monkeypatch.setattr(tasks, "_publisher", Mock(return_value=publisher))
    if outcome == "failed":
        with pytest.raises(RuntimeError, match="Channel publishing failed"):
            tasks.poll_telegram(settings, None)
    else:
        tasks.poll_telegram(settings, None)
    publisher.publish_item.assert_called_once_with(approved_id)
    assert repo.list_approved_item_ids.call_args_list == [
        call(source="telegram"),
        call(source="own_video"),
    ]


@pytest.mark.parametrize("key", [None, "test-api-key"])
def test_poll_only_creates_translation_client_with_key(
    settings, repo, api, inbox, monkeypatch, key
):
    read_token = Mock()
    monkeypatch.setattr(tasks, "_read_token", read_token)
    settings = replace(settings, anthropic_api_key=key)
    repo.conn.transaction.side_effect = nullcontext
    repo.conn.execute.return_value.fetchone.return_value = None
    repo.apply_pending_edit.return_value = None
    repo.create_inbox_item.return_value = None
    client = Mock()
    client.return_value.complete.return_value = "Mein Text"
    monkeypatch.setattr(tasks, "ClaudeClient", client)
    updates = [
        {
            "update_id": 50,
            "message": {
                "from": {"id": settings.telegram_chat_id},
                "chat": {"id": settings.telegram_chat_id},
                "text": "Мій текст",
            },
        }
    ]

    inbox.pending.return_value = [(update["update_id"], update) for update in updates]
    tasks.poll_telegram(settings, None)

    read_token.assert_not_called()
    if key is None:
        client.assert_not_called()
        repo.create_inbox_item.assert_not_called()
        api.send_message.assert_called_once_with(
            settings.telegram_chat_id, "Translation is not configured."
        )
    else:
        client.assert_called_once_with(key)
        repo.create_inbox_item.assert_called_once_with(50, "Мій текст", "Mein Text")


@pytest.mark.parametrize("source", ["telegram", "own_video"])
@pytest.mark.parametrize("expired", [True, False])
def test_linkedin_provider_and_expiry_guard(
    settings, repo, monkeypatch, expired, source
):
    now = datetime.now(UTC)
    monkeypatch.setattr(tasks, "_now", lambda: now)
    item = Item(uuid4(), source, ItemState.PUBLISHING, now, now, 1, now)
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
    monkeypatch.setattr(
        tasks,
        "OwnVideoStore",
        Mock(
            return_value=Mock(
                get_by_item=Mock(
                    return_value=SimpleNamespace(
                        reels_eligible=False, has_description=True
                    )
                )
            )
        ),
    )

    def reject_http(request):
        pytest.fail("A held publication must not make an HTTP request")

    with httpx.Client(transport=httpx.MockTransport(reject_http)) as client:
        publisher = tasks._publisher(settings, repo, client)
        report = publisher.publish_item(item.id)

    expected = {"linkedin": "held"}
    if source == "own_video":
        expected["threads"] = "held"
    assert {
        name: result.outcome for name, result in report.channels.items()
    } == expected
    assert [call.args for call in store.get.call_args_list] == [
        ("linkedin",),
    ]
    repo.claim_publication.assert_not_called()


def test_text_publisher_holds_only_reels_for_no_description(
    settings, repo, monkeypatch
):
    now = datetime.now(UTC)
    item = Item(uuid4(), "own_video", ItemState.PUBLISHING, now, now, 1, now)
    repo.get_item.return_value = item
    repo.get_variants.return_value = []
    repo.list_publications.return_value = []
    monkeypatch.setattr(
        tasks,
        "OwnVideoStore",
        Mock(
            **{
                "return_value.get_by_item.return_value": SimpleNamespace(
                    has_description=False
                )
            }
        ),
    )
    read_token = Mock()
    monkeypatch.setattr(tasks, "_read_token", read_token)
    with httpx.Client() as client:
        report = tasks._publisher(settings, repo, client).publish_item(item.id)

    assert {name: result.outcome for name, result in report.channels.items()} == {
        "instagram-reels": "held",
        "facebook-reels": "held",
        "youtube": "held",
        "tiktok": "held",
    }
    repo.claim_publication.assert_not_called()
    read_token.assert_not_called()


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
def test_poll_engagement_purges_with_no_sources(settings, monkeypatch, expired):
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
    engagement = Mock()
    monkeypatch.setattr(tasks, "EngagementStore", Mock(return_value=engagement))
    tasks.poll_engagement(replace(settings, github_owner=""), Mock())
    engagement.purge_personal_data.assert_called_once()
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
    collect = Mock(return_value=0)
    monkeypatch.setattr(tasks, "collect_threads", collect)
    monkeypatch.setattr(tasks, "ClaudeClient", Mock())
    engagement = Mock()
    engagement.list_unnotified.return_value = []
    monkeypatch.setattr(tasks, "EngagementStore", Mock(return_value=engagement))
    conn = Mock()
    tasks.poll_engagement(
        replace(settings, anthropic_api_key="fixture", github_owner=""), conn
    )
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
        github_owner="",
        imap=ImapSettings("mailbox@example.test", "fixture"),
        anthropic_api_key="fixture",
    )

    with pytest.raises(
        RuntimeError, match="^Engagement source or notification delivery failed$"
    ):
        tasks.poll_engagement(configured, conn)

    linkedin.assert_called_once()
    engagement.list_unnotified.assert_called_once_with()


@pytest.mark.parametrize(
    "purge_fails, github_fails", [(False, False), (True, False), (False, True)]
)
def test_poll_engagement_purges_before_sources_and_isolates_failure(
    settings, monkeypatch, caplog, purge_fails, github_fails
):
    actions = Mock()
    actions.list_unnotified.return_value = []
    actions.purge_personal_data.return_value = 2
    if purge_fails:
        actions.purge_personal_data.side_effect = RuntimeError("private event data")
    monkeypatch.setattr(tasks, "EngagementStore", Mock(return_value=actions))
    monkeypatch.setattr(tasks, "_read_token", Mock(return_value="stored"))
    monkeypatch.setattr(tasks, "ClaudeClient", Mock())
    monkeypatch.setattr(tasks, "collect_linkedin", actions.linkedin)
    monkeypatch.setattr(tasks, "collect_threads", actions.threads)
    monkeypatch.setattr(tasks, "collect_github", actions.github)
    github = Mock()
    monkeypatch.setattr(tasks, "GitHubClient", Mock(return_value=github))
    if github_fails:
        actions.github.side_effect = RuntimeError("private GitHub data")
    configured = replace(
        settings,
        github_token="test-token",
        imap=ImapSettings("mailbox@example.test", "fixture"),
        anthropic_api_key="fixture",
    )
    if purge_fails or github_fails:
        with pytest.raises(RuntimeError, match="Engagement source or notification"):
            tasks.poll_engagement(configured, Mock())
    else:
        tasks.poll_engagement(configured, Mock())

    assert [call[0] for call in actions.mock_calls] == [
        "purge_personal_data",
        "linkedin",
        "threads",
        "github",
        "list_unnotified",
    ]
    assert tasks.GitHubClient.call_args.args == ("schemchuk", "test-token")
    assert actions.github.call_args.args[:2] == (github, "schemchuk")
    assert "private GitHub data" not in caplog.text
    assert "private event data" not in caplog.text


def test_poll_engagement_runs_github_without_drafter_or_token(settings, monkeypatch):
    monkeypatch.setattr(tasks, "_read_token", Mock(return_value=None))
    collect = Mock()
    monkeypatch.setattr(tasks, "collect_github", collect)
    drafter = Mock()
    monkeypatch.setattr(tasks, "ReplyDrafter", drafter)
    store = Mock()
    store.list_unnotified.return_value = []
    monkeypatch.setattr(tasks, "EngagementStore", Mock(return_value=store))
    conn = Mock()

    tasks.poll_engagement(settings, conn)

    client, owner, state, actual_store = collect.call_args.args
    assert isinstance(client, tasks.GitHubClient)
    assert owner == settings.github_owner
    assert state.conn is conn
    assert actual_store is store
    drafter.assert_not_called()
    store.list_unnotified.assert_called_once()


@pytest.mark.parametrize(
    "mode", ["ok", "missing", "no-key", "github-error", "ai-error", "upload-error"]
)
def test_poll_advice_after_approval_and_publish(
    settings, repo, api, inbox, monkeypatch, caplog, mode
):
    from postpilot.notify.telegram import TelegramError

    private = "Private profile details"
    settings = replace(
        settings,
        anthropic_api_key=None if mode == "no-key" else "fake-key",
        github_owner="portfolio-owner",
        github_token="fake-github-key",
    )
    now = datetime(2025, 1, 2, tzinfo=UTC)
    monkeypatch.setattr(tasks, "_now", lambda: now)
    item_id = uuid4()
    repo.get_item.return_value = Item(
        item_id, "telegram", ItemState.AWAITING_APPROVAL, now, now, None, None
    )
    repo.bot_state.get_linkedin_profile = Mock(
        return_value=None if mode == "missing" else private
    )
    repo.list_approved_item_ids.side_effect = lambda **_: (
        [item_id] if repo.transition.called and _.get("source") == "telegram" else []
    )
    publisher = Mock()
    publisher.publish_item.return_value = PublishReport({})
    monkeypatch.setattr(tasks, "_publisher", Mock(return_value=publisher))

    chat = {"id": settings.telegram_chat_id}
    advice = {"from": chat, "chat": chat, "text": "/advise@ExampleBot"}
    updates = [
        {"update_id": 20, "message": advice},
        {"update_id": 21, "message": advice},
        {
            "update_id": 22,
            "callback_query": {
                "from": chat,
                "id": "approve",
                "data": f"approve:{item_id}",
                "message": {"chat": chat, "message_id": 1},
            },
        },
    ]
    inbox.pending.return_value = [(update["update_id"], update) for update in updates]
    github_calls = []
    model_calls = []

    def respond(request):
        publisher.publish_item.assert_called_once_with(item_id)
        assert inbox.mark_processed.call_args_list == [call(20), call(21), call(22)]
        assert request.url.path == "/users/portfolio-owner/repos"
        assert request.headers["Authorization"] == "Bearer fake-github-key"
        github_calls.append(request.url.path)
        return httpx.Response(500 if mode == "github-error" else 200, json=[])

    class FakeModel:
        def complete(self, system, user, *, max_tokens):
            model_calls.append(max_tokens)
            if mode == "ai-error":
                raise RuntimeError(private)
            return "# Поради"

    if mode == "upload-error":
        api.send_document.side_effect = TelegramError(private)
        api.send_message.side_effect = TelegramError(private)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(tasks.httpx, "Client", lambda: client)
    monkeypatch.setattr(tasks, "ClaudeClient", lambda _: FakeModel())
    tasks.poll_telegram(settings, None)

    publisher.publish_item.assert_called_once_with(item_id)
    repo.bot_state.get_linkedin_profile.assert_called_once()
    assert private not in caplog.text
    if mode in {"missing", "no-key"}:
        assert not github_calls
        assert not model_calls
        api.send_document.assert_not_called()
        expected = (
            "No profile stored. Send /profile <text> first."
            if mode == "missing"
            else "Profile advice requires an AI key."
        )
        api.send_message.assert_called_once_with(chat["id"], expected)
    else:
        assert len(github_calls) == 1
        assert model_calls == ([] if mode == "github-error" else [8000])
        if mode in {"ok", "upload-error"}:
            api.send_document.assert_called_once_with(
                chat["id"],
                "profile-advice-2025-01-02.md",
                "# Поради".encode(),
                caption="Profile advice",
            )
        else:
            api.send_document.assert_not_called()
        if mode == "ok":
            api.send_message.assert_not_called()
        else:
            api.send_message.assert_called_once_with(chat["id"], "Advice failed")
            assert "Profile advisor failed (" in caplog.text


def test_snapshot_traffic_skips_inaccessible_repos(settings, monkeypatch, capsys):
    settings = replace(settings, github_token="mock-traffic-token")
    monkeypatch.setattr(tasks, "_now", lambda: datetime(2026, 11, 21, tzinfo=UTC))
    store = Mock()
    store.list_links.return_value = []
    monkeypatch.setattr(tasks, "LinkStore", Mock(return_value=store))
    monkeypatch.setattr(
        tasks,
        "SeriesStore",
        Mock(return_value=Mock(posted_repos=Mock(return_value={"private", "gone"}))),
    )
    github = Mock()
    github.traffic.return_value = None
    monkeypatch.setattr(tasks, "GitHubClient", Mock(return_value=github))

    tasks.snapshot_traffic(settings, Mock())

    store.upsert_traffic.assert_not_called()
    out = capsys.readouterr().out
    assert "repo-not-accessible repo=gone" in out
    assert "repo-not-accessible repo=private" in out
    assert "summary repos=2 links=0 saved=False" in out


@pytest.mark.parametrize("telegram_error", [False, True])
def test_publish_failure_notification(telegram_error, caplog):
    item_id = uuid4()
    repo = Mock()
    repo.get_item.return_value = SimpleNamespace(id=item_id, source="telegram")
    repo.list_publications.return_value = [
        SimpleNamespace(channel=channel, error=error, status=status)
        for channel, error, status in [
            ("linkedin", "<error>" + "x" * 300, PublicationStatus.FAILED),
            ("threads", "unavailable", PublicationStatus.FAILED),
            ("linkedin-ua", "paused error", PublicationStatus.FAILED),
            ("facebook-reels", None, PublicationStatus.PUBLISHED),
        ]
    ]
    publisher = Mock(repo=repo)
    failure = PublishReport(
        {"linkedin": ChannelResult("failed")}, transitioned_to_failed=True
    )
    untouched = PublishReport({"linkedin": ChannelResult("held")})
    publisher.publish_item.side_effect = [failure, untouched, failure]
    api = Mock()
    if telegram_error:
        api.send_message.side_effect = TelegramError("private request details")
    notifier = Notifier(api, 123)

    with pytest.raises(RuntimeError, match="^Channel publishing failed$"):
        tasks._publish_items(publisher, [item_id, item_id], notifier=notifier)
    expected = call(
        123,
        f"Publishing failed · {str(item_id)[:8]} · telegram\n"
        f"linkedin: <error>{'x' * 293}\nthreads: unavailable",
        {
            "inline_keyboard": [
                [
                    {"text": "Retry", "callback_data": f"retry:{item_id}"},
                    {"text": "Skip", "callback_data": f"skipfail:{item_id}"},
                ]
            ]
        },
    )
    assert api.send_message.call_args_list == [expected]

    with pytest.raises(RuntimeError, match="^Channel publishing failed$"):
        tasks._publish_items(publisher, [item_id], notifier=notifier)
    assert api.send_message.call_args_list == [expected, expected]
    assert ("Telegram publishing failure notification failed" in caplog.text) == (
        telegram_error
    )
    assert "private request details" not in caplog.text


@pytest.mark.parametrize("source", ["telegram", "own_video"])
def test_poll_processes_failed_retry_and_next_update(
    settings, repo, inbox, api, monkeypatch, caplog, source
):
    item_id, next_item_id = uuid4(), uuid4()
    publisher = Mock(repo=repo)
    own_video = source == "own_video"
    report = (
        PublishReport(
            {
                "instagram-reels": ChannelResult("held"),
                "facebook-reels": ChannelResult("held"),
            }
        )
        if own_video
        else PublishReport(
            {"linkedin": ChannelResult("failed")}, transitioned_to_failed=True
        )
    )
    repo.get_item.return_value = SimpleNamespace(id=item_id, source=source)
    repo.list_publications.return_value = [
        SimpleNamespace(
            channel="linkedin", status=PublicationStatus.FAILED, error="rejected"
        )
    ]
    text_publisher = Mock(return_value=publisher)
    monkeypatch.setattr(tasks, "_publisher", text_publisher)
    reel_channels = Mock()
    monkeypatch.setattr(tasks, "_reel_channels", reel_channels)
    inbox.pending.return_value = [
        (
            41,
            {
                "update_id": 41,
                "callback_query": {
                    "from": {"id": settings.telegram_chat_id},
                    "id": "retry-button",
                    "data": f"retry:{item_id}",
                    "message": {
                        "chat": {"id": settings.telegram_chat_id},
                        "message_id": 11,
                    },
                },
            },
        ),
        (
            42,
            {
                "update_id": 42,
                "callback_query": {
                    "from": {"id": settings.telegram_chat_id},
                    "id": "skip-button",
                    "data": f"skipfail:{next_item_id}",
                    "message": {
                        "chat": {"id": settings.telegram_chat_id},
                        "message_id": 12,
                    },
                },
            },
        ),
    ]

    def publish(id):
        repo.resolve_failed_item.assert_called_once_with(id, ItemState.PUBLISHING, 11)
        api.send_message.assert_called_once_with(
            settings.telegram_chat_id, "Retrying", reply_to_message_id=11
        )
        return report

    publisher.publish_item.side_effect = publish

    tasks.poll_telegram(settings, None)

    inbox.record_failure.assert_not_called()
    assert inbox.mark_processed.call_args_list == [call(41), call(42)]
    repo.resolve_failed_item.assert_called_with(next_item_id, ItemState.SKIPPED, 12)
    api.answer_callback_query.assert_not_called()
    api.send_message.assert_called_with(
        settings.telegram_chat_id, "Skipped", reply_to_message_id=12
    )
    text_publisher.assert_called_once()
    publisher.publish_item.assert_called_once_with(item_id)
    reel_channels.assert_not_called()
    if own_video:
        assert not caplog.messages
        assert api.send_message.call_count == 2
        repo.list_publications.assert_not_called()
    else:
        assert caplog.messages == [
            f"Channel publishing failed after Telegram retry (item {item_id})"
        ]
        assert api.send_message.call_count == 3
        assert (
            api.send_message.call_args_list[1].args[1].endswith("\nlinkedin: rejected")
        )
