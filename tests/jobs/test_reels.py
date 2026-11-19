from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from postpilot.channels.base import (
    ChannelError,
    ChannelHeld,
    TransientChannelError,
    UncertainPublication,
)
from postpilot.config import FacebookSettings, Settings
from postpilot.jobs import tasks
from postpilot.video.publisher import publish_one
from postpilot.video.store import VideoStatus


@pytest.fixture
def settings():
    return Settings("db", "bot", 1, "key", facebook=FacebookSettings("123"))


@pytest.mark.parametrize("retry", [False, True])
def test_job_wires_facebook_and_retry(monkeypatch, settings, retry):
    now = datetime.now(UTC)
    stored = SimpleNamespace(
        access_token="test-token", expires_at=now + timedelta(days=1)
    )
    tokens = Mock()
    tokens.get.side_effect = lambda provider: stored if provider == "facebook" else None
    monkeypatch.setattr(tasks, "TokenStore", Mock(return_value=tokens))
    monkeypatch.setattr(tasks, "TokenCipher", Mock())
    monkeypatch.setattr(tasks, "publish_one", Mock())
    conn = Mock()
    tasks.publish_reels(settings, conn, retry=retry)
    store, renders, channels, read_token, _ = tasks.publish_one.call_args.args
    assert store.conn is renders.conn is conn
    assert [channel.name for channel in channels] == ["facebook-reels"]
    assert read_token("facebook") == "test-token"
    assert tasks.publish_one.call_args.kwargs == {"retry": retry}


def test_facebook_import_stores_token_with_placeholder_expiry(monkeypatch, settings):
    now = datetime.now(UTC)
    store = Mock()
    monkeypatch.setattr(tasks, "_now", lambda: now)
    monkeypatch.setattr(tasks, "TokenStore", Mock(return_value=store))
    monkeypatch.setattr(tasks, "TokenCipher", Mock())
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "test-token&value")
    tasks.facebook_import_token(settings, Mock())
    store.save.assert_called_once_with(
        "facebook", "test-token&value", now + timedelta(days=3650)
    )


@pytest.mark.parametrize("valid", [False, True])
def test_check_tokens_validates_facebook_without_refresh(monkeypatch, settings, valid):
    state = Mock()
    state.send_token_alert_once.side_effect = lambda provider, day, send: send()
    notifier = Mock()
    store = Mock()
    store.get.return_value = SimpleNamespace(access_token="test-token")
    monkeypatch.setattr(
        tasks, "ItemRepo", Mock(return_value=SimpleNamespace(bot_state=state))
    )
    monkeypatch.setattr(tasks, "Notifier", Mock(return_value=notifier))
    monkeypatch.setattr(tasks, "TokenStore", Mock(return_value=store))
    monkeypatch.setattr(tasks, "TokenCipher", Mock())

    def route(request):
        assert request.url.path.endswith("/debug_token")
        return httpx.Response(200, json={"data": {"is_valid": valid}})

    client = httpx.Client(transport=httpx.MockTransport(route))
    monkeypatch.setattr(tasks.httpx, "Client", lambda: client)
    tasks.check_tokens(settings, Mock())
    if valid:
        notifier.send_alert.assert_not_called()
    else:
        notifier.send_alert.assert_called_once_with(
            "facebook: Page token missing or invalid"
        )
    store.save.assert_not_called()
    notifier.send_renewal_prompt.assert_not_called()


def setup_publish(status):
    render = SimpleNamespace(id=uuid4(), status=status)
    claim = Mock(remote_ref=None)
    store = Mock()
    store.oldest_eligible.return_value = render.id
    store.claim.return_value = claim
    renders = Mock()
    renders.get.return_value = render
    return store, renders, claim


def channel(name, provider="token"):
    return SimpleNamespace(
        name=name,
        token_provider=provider,
        publish_with_ref=Mock(return_value="remote"),
        permalink=Mock(return_value="https://www.facebook.com/reel/remote"),
    )


def test_non_approved_render_never_published():
    store, renders, _ = setup_publish(VideoStatus.READY)
    facebook = channel("facebook-reels")
    publish_one(store, renders, [facebook], Mock(), Mock())
    store.claim.assert_not_called()
    facebook.publish_with_ref.assert_not_called()


def test_channel_failure_is_redacted_and_does_not_block_others():
    store, renders, claim = setup_publish(VideoStatus.APPROVED)
    first, second = channel("first"), channel("second")
    first.publish_with_ref.side_effect = ChannelError("secret information")
    notify = Mock()
    publish_one(store, renders, [first, second], lambda _: "token", notify)
    store.oldest_eligible.assert_called_once_with(
        {"first", "second"}, timedelta(minutes=15), retry=False
    )
    store.mark_failed.assert_called_once_with(
        claim, "Channel operation failed (ChannelError)"
    )
    store.mark_published.assert_called_once_with(claim, "remote")
    assert "secret information" not in str(notify.call_args_list)
    assert notify.call_count == 2
    assert "https://www.facebook.com/reel/remote" in notify.call_args.args[0]


@pytest.mark.parametrize("missing", [False, True])
def test_held_channel_releases_claim_and_alerts_once(missing):
    store, renders, claim = setup_publish(VideoStatus.APPROVED)
    channels = [channel("first"), channel("second")]
    for adapter in channels:
        adapter.publish_with_ref.side_effect = ChannelHeld
    notify = Mock()
    publish_one(
        store, renders, channels, lambda _: None if missing else "token", notify
    )
    assert store.release.call_count == 2
    store.release.assert_called_with(claim)
    notify.assert_called_once_with("Reels held: missing or invalid token for token")
    store.mark_failed.assert_not_called()


@pytest.mark.parametrize("error", [None, UncertainPublication, TransientChannelError])
def test_pending_has_no_alert_or_final_result(error):
    store, renders, _ = setup_publish(VideoStatus.APPROVED)
    facebook = channel("facebook-reels")
    facebook.publish_with_ref.return_value = None
    if error is not None:
        facebook.publish_with_ref.side_effect = error
    notify = Mock()
    publish_one(store, renders, [facebook], lambda _: "token", notify)
    notify.assert_not_called()
    store.mark_published.assert_not_called()
    store.mark_failed.assert_not_called()


def test_retry_applies_to_selection_and_claim():
    store, renders, claim = setup_publish(VideoStatus.APPROVED)
    facebook = channel("facebook-reels")
    claim.remote_ref = "saved-id"
    publish_one(store, renders, [facebook], lambda _: "token", Mock(), retry=True)
    stale_after = timedelta(minutes=15)
    store.oldest_eligible.assert_called_once_with(
        {"facebook-reels"}, stale_after, retry=True
    )
    store.claim.assert_called_once_with(
        renders.get.return_value.id, "facebook-reels", stale_after, retry=True
    )
    assert facebook.publish_with_ref.call_args.args[2] == "saved-id"
