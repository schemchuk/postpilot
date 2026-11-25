from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, Mock
from uuid import uuid4

import httpx
import pytest

from postpilot.channels.base import (
    ChannelError,
    ChannelHeld,
    TransientChannelError,
    UncertainPublication,
)
from postpilot.config import (
    FacebookSettings,
    InstagramSettings,
    LinkedInSettings,
    Settings,
)
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
    conn.transaction.side_effect = nullcontext
    conn.execute.return_value.fetchall.return_value = []
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
    render = SimpleNamespace(
        id=uuid4(),
        item_id=uuid4(),
        status=status,
        video=b"video",
        script="German script",
        duration=20,
    )
    claim = Mock(
        remote_ref=None, remote_ref_claimed_at=None, updated_at=datetime.now(UTC)
    )
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


def test_channel_failure_is_reported_and_does_not_block_others(capsys):
    store, renders, claim = setup_publish(VideoStatus.APPROVED)
    first, second = channel("first"), channel("second")
    first.publish_with_ref.side_effect = ChannelError(
        "Reels request rejected (HTTP 400)"
    )
    notify = Mock()
    publish_one(store, renders, [first, second], lambda _: "token", notify)
    store.oldest_eligible.assert_called_once_with(
        {"first", "second"}, timedelta(minutes=15), retry=False
    )
    store.mark_failed.assert_called_once_with(
        claim, "ChannelError: Reels request rejected (HTTP 400)"
    )
    store.mark_published.assert_called_once_with(claim, "remote")
    reel = second.publish_with_ref.call_args.args[0]
    assert (
        reel.caption == "German script\n\nKI-generierter Avatar / AI-generated avatar"
    )
    assert reel.ai_generated is True
    assert notify.call_args_list[0].args == (
        "first: " + store.mark_failed.call_args.args[1],
    )
    item_id = renders.get.return_value.item_id
    failed_lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("trace publish failed")
    ]
    assert failed_lines == [
        (
            f"trace publish failed item={item_id} channel=first "
            "reason=ChannelError:_Reels_request_rejected_(HTTP_400)"
        )
    ]
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
    claim.remote_ref_claimed_at = claim.updated_at - timedelta(hours=2)
    publish_one(store, renders, [facebook], lambda _: "token", Mock(), retry=True)
    stale_after = timedelta(minutes=15)
    store.oldest_eligible.assert_called_once_with(
        {"facebook-reels"}, stale_after, retry=True
    )
    store.claim.assert_called_once_with(
        renders.get.return_value.id, "facebook-reels", stale_after, retry=True
    )
    assert facebook.publish_with_ref.call_args.args[2] == "saved-id"
    assert facebook.publish_with_ref.call_args.kwargs == {
        "claimed_at": claim.remote_ref_claimed_at
    }


@pytest.mark.parametrize("facebook", [True, False])
@pytest.mark.parametrize("fail_facebook", [True, False])
def test_configured_reels_channels_use_independent_claims_and_tokens(
    monkeypatch, settings, facebook, fail_facebook
):
    settings = replace(
        settings,
        instagram=InstagramSettings("ig"),
        facebook=settings.facebook if facebook else None,
    )
    store, renders, _ = setup_publish(VideoStatus.APPROVED)
    adapters = {
        name: channel(f"{name}-reels", name) for name in ("facebook", "instagram")
    }
    claims = {
        adapter.name: Mock(
            remote_ref=None, remote_ref_claimed_at=None, updated_at=datetime.now(UTC)
        )
        for adapter in adapters.values()
    }
    store.claim.side_effect = lambda _, name, stale, **options: claims[name]
    monkeypatch.setattr(tasks, "VideoPublicationStore", Mock(return_value=store))
    monkeypatch.setattr(tasks, "VideoStore", Mock(return_value=renders))
    monkeypatch.setattr(
        tasks, "FacebookReelsChannel", Mock(return_value=adapters["facebook"])
    )
    monkeypatch.setattr(
        tasks, "InstagramReelsChannel", Mock(return_value=adapters["instagram"])
    )
    monkeypatch.setattr(tasks, "Notifier", Mock())
    monkeypatch.setattr(tasks, "TokenCipher", Mock())
    tokens = Mock()
    tokens.get.side_effect = lambda provider: SimpleNamespace(
        access_token=f"{provider}-token",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    monkeypatch.setattr(tasks, "TokenStore", Mock(return_value=tokens))
    if fail_facebook:
        adapters["facebook"].publish_with_ref.side_effect = ChannelError

    conn = Mock()
    conn.transaction.side_effect = nullcontext
    conn.execute.return_value.fetchall.return_value = []
    tasks.publish_reels(settings, conn)

    for provider, adapter in adapters.items():
        if provider == "facebook" and not facebook:
            adapter.publish_with_ref.assert_not_called()
            continue
        assert adapter.publish_with_ref.call_args.args[1] == f"{provider}-token"
        claim = claims[adapter.name]
        assert adapter.publish_with_ref.call_args.kwargs == {
            "claimed_at": claim.updated_at
        }
        if provider == "facebook" and fail_facebook:
            store.mark_failed.assert_called_once_with(claim, "ChannelError:")
        else:
            assert (claim, "remote") in [
                call.args for call in store.mark_published.call_args_list
            ]


@pytest.mark.parametrize(
    "days, data_days, expected",
    [
        (0, 90, 90),
        (90, 45, 45),
        (45, 90, 45),
        (0, 0, 60),
        (90, 0, 90),
        (None, None, 60),
    ],
)
@pytest.mark.parametrize("valid", [True, False])
def test_instagram_import_validates_and_stores_earliest_expiry(
    monkeypatch, settings, days, data_days, expected, valid
):
    settings = replace(settings, instagram=InstagramSettings("ig"))
    now = datetime.now(UTC)
    store = Mock()
    monkeypatch.setattr(tasks, "_now", lambda: now)
    monkeypatch.setattr(tasks, "TokenStore", Mock(return_value=store))
    monkeypatch.setattr(tasks, "TokenCipher", Mock())
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "test-token&value")
    debug = {"is_valid": valid}
    for key, value in (("expires_at", days), ("data_access_expires_at", data_days)):
        if value is not None:
            debug[key] = int((now + timedelta(days=value)).timestamp()) if value else 0

    def route(request):
        assert request.url.path == "/v27.0/debug_token"
        assert request.url.params["input_token"] == "test-token&value"
        return httpx.Response(200, json={"data": debug})

    client = httpx.Client(transport=httpx.MockTransport(route))
    monkeypatch.setattr(tasks.httpx, "Client", lambda: client)
    if not valid:
        with pytest.raises(ValueError, match="Instagram token is invalid"):
            tasks.instagram_import_token(settings, Mock())
        store.save.assert_not_called()
    else:
        tasks.instagram_import_token(settings, Mock())
        expiry = now if not days and not data_days else now.replace(microsecond=0)
        store.save.assert_called_once_with(
            "instagram", "test-token&value", expiry + timedelta(days=expected)
        )


@pytest.mark.parametrize(
    "days, valid, message",
    [
        (None, True, "instagram: token missing or invalid"),
        (30, False, "instagram: token missing or invalid"),
        (0, True, "instagram: token missing or invalid"),
        (14, True, "instagram: token expires in 14 day(s); re-import manually"),
        (15, True, None),
    ],
)
def test_check_instagram_tokens_alerts_once_daily(
    monkeypatch, settings, days, valid, message
):
    settings = replace(settings, facebook=None, instagram=InstagramSettings("ig"))
    now = datetime.now(UTC)
    monkeypatch.setattr(tasks, "_now", lambda: now)
    state = Mock()
    notifier = Mock()
    store = Mock()
    store.get.return_value = (
        None
        if days is None
        else SimpleNamespace(
            access_token="test-token", expires_at=now + timedelta(days=days)
        )
    )
    monkeypatch.setattr(
        tasks, "ItemRepo", Mock(return_value=SimpleNamespace(bot_state=state))
    )
    monkeypatch.setattr(tasks, "Notifier", Mock(return_value=notifier))
    monkeypatch.setattr(tasks, "TokenStore", Mock(return_value=store))
    monkeypatch.setattr(tasks, "TokenCipher", Mock())
    checked = Mock(return_value=valid)
    monkeypatch.setattr(tasks, "token_is_valid", checked)

    tasks.check_tokens(settings, Mock())

    if days is not None:
        checked.assert_called_once_with(settings.instagram, ANY, "test-token")
    else:
        checked.assert_not_called()
    if message:
        state.send_token_alert_once.assert_called_once()
        provider, day, send = state.send_token_alert_once.call_args.args
        assert (provider, day) == ("instagram", now.date())
        send()
        notifier.send_alert.assert_called_once_with(message)
    else:
        state.send_token_alert_once.assert_not_called()
    store.save.assert_not_called()
    notifier.send_renewal_prompt.assert_not_called()


@pytest.mark.parametrize("has_token", [True, False])
def test_job_publishes_heygen_with_linkedin_only(monkeypatch, has_token):

    settings = Settings(
        "db", "bot", 1, "key", linkedin=LinkedInSettings("id", "secret", "uri", ())
    )
    store, renders, claim = setup_publish(VideoStatus.APPROVED)
    monkeypatch.setattr(tasks, "VideoPublicationStore", Mock(return_value=store))
    monkeypatch.setattr(tasks, "VideoStore", Mock(return_value=renders))
    monkeypatch.setattr(tasks, "TokenCipher", Mock())
    monkeypatch.setattr(tasks, "Notifier", Mock())
    monkeypatch.setattr(tasks, "OwnVideoStore", Mock())
    publish = Mock(return_value="urn:li:share:1")
    monkeypatch.setattr(tasks.LinkedInRenderChannel, "publish_render", publish)
    token = SimpleNamespace(
        access_token="test-token",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    tokens = Mock()
    tokens.get.return_value = token if has_token else None
    monkeypatch.setattr(tasks, "TokenStore", Mock(return_value=tokens))

    tasks.publish_reels(settings, Mock())

    store.oldest_eligible.assert_called_once_with(
        {"linkedin-video"}, timedelta(minutes=15), retry=False
    )
    assert [call.args[1] for call in store.claim.call_args_list] == [
        "linkedin-video",
    ]
    assert len(tokens.get.call_args_list) == 1
    tokens.get.assert_called_with("linkedin")
    tasks.OwnVideoStore.assert_not_called()
    if has_token:
        assert publish.call_count == 1
        assert publish.call_args.args[:3] == (
            renders.get.return_value,
            "test-token",
            None,
        )
        assert store.mark_published.call_count == 1
        store.mark_published.assert_called_with(claim, "urn:li:share:1")
    else:
        publish.assert_not_called()
        assert store.release.call_count == 1
        store.mark_published.assert_not_called()
