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
    TikTokSettings,
)
from postpilot.core.models import Variant
from postpilot.jobs import tasks
from postpilot.tokens.tiktok import TikTokTokenError, TikTokTokenUnavailable
from postpilot.video.label import AI_VIDEO_CAPTION_LABEL
from postpilot.video.publisher import publish_one
from postpilot.video.store import VideoStatus


@pytest.fixture(autouse=True)
def disable_project_scan(monkeypatch):
    monkeypatch.setattr(tasks, "scan_projects", lambda *args, **kwargs: True)


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

    conn = Mock()
    conn.transaction.side_effect = nullcontext
    conn.execute.return_value.fetchall.return_value = []
    tasks.publish_reels(settings, conn)

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


def test_youtube_routes_only_own_and_factory_and_shares_token_and_budget(
    monkeypatch, settings
):
    from postpilot.config import YouTubeSettings

    settings = replace(settings, youtube=YouTubeSettings("id", "secret"))
    conn = Mock()
    conn.transaction.side_effect = nullcontext
    conn.execute.return_value.fetchall.return_value = [(uuid4(),)]
    monkeypatch.setattr(tasks, "TokenStore", Mock())
    monkeypatch.setattr(tasks, "TokenCipher", Mock())
    monkeypatch.setattr(tasks, "publish_one", Mock())
    monkeypatch.setattr(tasks, "_publish_items", Mock())
    manager = Mock(**{"return_value.access_token.return_value": "access"})
    monkeypatch.setattr(tasks, "YouTubeTokenManager", manager)
    tasks.publish_reels(settings, conn)
    assert [channel.name for channel in tasks.publish_one.call_args.args[2]] == [
        "facebook-reels"
    ]
    own, factory = [call.args[0] for call in tasks._publish_items.call_args_list]
    own_youtube = next(ch for ch in own.own_video_channels if ch.name == "youtube")
    factory_youtube = next(
        ch for ch in factory.project_channels if ch.name == "youtube"
    )
    assert own_youtube._channel is factory_youtube._channel
    assert own_youtube._read_token("youtube") == "access"
    assert factory_youtube._read_token("youtube") == "access"
    manager.return_value.access_token.assert_called_once_with()


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("error", [None, TikTokTokenError, TikTokTokenUnavailable])
def test_tiktok_routes_only_own_and_factory_and_caches_token(
    monkeypatch, settings, paused, error
):
    settings = replace(
        settings, tiktok=TikTokSettings("sandbox", "key", "secret", "callback")
    )
    conn = Mock()
    conn.transaction.side_effect = nullcontext
    conn.execute.return_value.fetchall.return_value = [(uuid4(),)]
    monkeypatch.setattr(tasks, "TokenStore", Mock())
    monkeypatch.setattr(tasks, "TokenCipher", Mock())
    monkeypatch.setattr(tasks, "publish_one", Mock())
    monkeypatch.setattr(tasks, "_publish_items", Mock())
    monkeypatch.setattr(tasks, "is_paused", lambda name: paused and name == "tiktok")
    manager = Mock(**{"return_value.access_token.return_value": "access"})
    manager.return_value.access_token.side_effect = error
    monkeypatch.setattr(tasks, "TikTokTokenManager", manager)
    constructor = Mock(wraps=tasks.TikTokChannel)
    constructor.name = "tiktok"
    constructor.token_provider = "tiktok"
    monkeypatch.setattr(tasks, "TikTokChannel", constructor)

    tasks.publish_reels(settings, conn)

    assert [channel.name for channel in tasks.publish_one.call_args.args[2]] == [
        "facebook-reels"
    ]
    own, factory = [call.args[0] for call in tasks._publish_items.call_args_list]
    assert "tiktok" in own.own_video_held_channels
    if paused:
        constructor.assert_not_called()
        manager.assert_not_called()
        assert all(ch.name != "tiktok" for ch in own.own_video_channels)
        assert all(ch.name != "tiktok" for ch in factory.project_channels)
        return

    constructor.assert_called_once()
    own_tiktok = next(ch for ch in own.own_video_channels if ch.name == "tiktok")
    factory_tiktok = next(ch for ch in factory.project_channels if ch.name == "tiktok")
    assert own_tiktok._channel is factory_tiktok._channel
    assert own_tiktok._caption_limit is None
    assert factory_tiktok._caption_limit is None
    assert factory_tiktok._use_release_video
    if error is None:
        assert own_tiktok._read_token("tiktok") == "access"
        assert factory.token_guard("tiktok")
        assert factory_tiktok._read_token("tiktok") == "access"
    else:
        with pytest.raises(ChannelHeld):
            own_tiktok.publish_with_ref(
                Mock(), [], ref=None, claimed_at=datetime.now(UTC), save_ref=Mock()
            )
        assert not factory.token_guard("tiktok")
    manager.return_value.access_token.assert_called_once_with()


@pytest.mark.parametrize(
    ("source", "description", "ai"),
    [
        ("own_video", True, True),
        ("own_video", False, True),
        ("own_video", False, False),
        ("project", True, True),
    ],
)
def test_tiktok_workflow_reports_caption_from_own_and_factory_adapters(
    monkeypatch, settings, source, description, ai
):
    item_id = uuid4()
    settings = replace(
        settings, tiktok=TikTokSettings("sandbox", "key", "secret", "callback")
    )
    conn = Mock()
    conn.transaction.side_effect = nullcontext
    conn.execute.return_value.fetchall.return_value = [(item_id,)]
    telegram = Mock()
    text = "  " + "Auf Deutsch. " * 200
    monkeypatch.setattr(tasks, "TokenStore", Mock())
    monkeypatch.setattr(tasks, "TokenCipher", Mock())
    monkeypatch.setattr(tasks, "publish_one", Mock())
    monkeypatch.setattr(tasks, "TelegramApi", Mock(return_value=telegram))
    monkeypatch.setattr(
        tasks,
        "TikTokTokenManager",
        Mock(**{"return_value.access_token.return_value": "access"}),
    )
    monkeypatch.setattr(
        tasks,
        "OwnVideoStore",
        Mock(
            **{
                "return_value.get_by_item.return_value": SimpleNamespace(
                    has_description=description, duration=10, ai_generated=ai
                )
            }
        ),
    )
    assets = Mock()
    assets.reel.return_value = SimpleNamespace(
        post_de=text, duration=10, ai=ai, title_de=None
    )
    monkeypatch.setattr(tasks, "FactoryAssets", Mock(return_value=assets))
    monkeypatch.setattr(
        tasks.ItemRepo,
        "list_publications",
        Mock(return_value=[SimpleNamespace(channel="tiktok", remote_id="inbox-id")]),
    )

    def route(request):
        assert request.url.path.endswith("/status/fetch/")
        return httpx.Response(
            200,
            json={"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}},
        )

    client = httpx.Client(transport=httpx.MockTransport(route))
    monkeypatch.setattr(tasks.httpx, "Client", lambda: client)

    def publish(publisher, ids, *, on_result, notifier, on_project_hold=None):
        if source == "project" and not publisher.project_channels:
            return
        if source == "own_video" and not publisher.own_video_channels:
            return
        assert item_id in ids
        channels = publisher.project_channels or publisher.own_video_channels
        channel = next(ch for ch in channels if ch.name == "tiktok")
        save_ref = Mock()
        assert (
            channel.publish_with_ref(
                SimpleNamespace(id=item_id, source=source),
                [Variant(item_id, "de", text)] if description else [],
                ref="inbox-id",
                claimed_at=datetime.now(UTC),
                save_ref=save_ref,
            )
            == "inbox-id"
        )
        save_ref.assert_not_called()
        on_result(item_id, "tiktok", tasks.ChannelResult("published"))

    monkeypatch.setattr(tasks, "_publish_items", publish)
    tasks.publish_reels(settings, conn)

    caption = text if description else ""
    if ai:
        caption = (
            f"{caption}\n\n{AI_VIDEO_CAPTION_LABEL}"
            if caption
            else AI_VIDEO_CAPTION_LABEL
        )
    heading = "tiktok: in your TikTok inbox, finish in the TikTok app."
    result = f"Caption to copy:\n{caption}" if caption else "No caption."
    if len(caption) > 2200:
        result += f"\nCaption is {len(caption)} chars, TikTok allows 2200."
    telegram.send_message.assert_called_once_with(1, f"{heading}\n{result}")


def test_project_daily_notices_are_deduplicated_on_berlin_day(monkeypatch, settings):
    from postpilot.core.approval import ProjectApprovalError, ProjectDailyLimitError
    from postpilot.projects import Project, Projects
    from postpilot.store.bot_state import BotState

    now = datetime(2026, 11, 27, 22, 59, tzinfo=UTC)
    monkeypatch.setattr(tasks, "_now", lambda: now)
    project = Project("demo", "owner/repo", ("reel",), ("threads",))
    disabled = replace(project, key="disabled", repo="owner/disabled", enabled=False)
    monkeypatch.setattr(tasks.Projects, "load", lambda: Projects((project, disabled)))
    conn = Mock()
    conn.transaction.side_effect = nullcontext
    seen = {}

    def execute(_query, values):
        key, day = values
        if seen.get(key, "") >= day:
            return Mock(rowcount=0)
        seen[key] = day
        return Mock(rowcount=1)

    conn.execute.side_effect = execute
    repo = Mock(bot_state=BotState(conn))
    repo.list_approved_item_ids.return_value = []
    repo.list_unfinished_item_ids.return_value = []
    monkeypatch.setattr(tasks, "ItemRepo", Mock(return_value=repo))
    store = Mock()
    limited, held = uuid4(), uuid4()
    store.list_due_item_ids.return_value = [limited, held]
    monkeypatch.setattr(tasks, "ProjectItemStore", Mock(return_value=store))
    message = "Project demo daily limit 1 reached; reel/demo waits until tomorrow"
    errors = {
        limited: ProjectDailyLimitError(message),
        held: ProjectApprovalError("Project 'demo' needs a tap"),
    }

    def publish(item_id):
        raise errors[item_id]

    publisher = Mock()
    publisher.publish_item.side_effect = publish
    monkeypatch.setattr(tasks, "Publisher", Mock(return_value=publisher))
    monkeypatch.setattr(tasks, "TokenCipher", Mock())
    monkeypatch.setattr(tasks, "publish_one", Mock())
    notifier = Mock()
    monkeypatch.setattr(tasks, "Notifier", Mock(return_value=notifier))
    tasks.publish_reels(settings, conn)
    tasks.publish_reels(settings, conn)
    assert [call.args[0] for call in notifier.send_alert.call_args_list] == [
        "Project disabled is disabled",
        message,
        f"Project item {held.hex[:8]} held: Project 'demo' needs a tap",
    ]
    now += timedelta(minutes=1)
    tasks.publish_reels(settings, conn)
    assert notifier.send_alert.call_count == 6


def test_invalid_projects_config_allows_own_video_job(monkeypatch, settings):
    from postpilot.projects import ProjectsConfigError

    monkeypatch.setattr(
        tasks.Projects, "load", Mock(side_effect=ProjectsConfigError("invalid"))
    )
    own_video = uuid4()
    repo = Mock()
    repo.bot_state.send_daily_once.side_effect = lambda key, day, send: send()
    repo.list_approved_item_ids.return_value = [own_video]
    repo.list_unfinished_item_ids.return_value = []
    monkeypatch.setattr(tasks, "ItemRepo", Mock(return_value=repo))
    monkeypatch.setattr(tasks, "TokenCipher", Mock())
    monkeypatch.setattr(tasks, "publish_one", Mock())
    publish_items = Mock()
    monkeypatch.setattr(tasks, "_publish_items", publish_items)
    notifier = Mock()
    monkeypatch.setattr(tasks, "Notifier", Mock(return_value=notifier))
    tasks.publish_reels(settings, Mock())

    tasks.publish_one.assert_called_once()
    publish_items.assert_called_once()
    assert list(publish_items.call_args.args[1]) == [own_video]
    notifier.send_alert.assert_called_once_with(
        "Project publishing held: config/projects.toml is invalid."
    )


@pytest.mark.parametrize("result", ["ok", "missing", "error", "draft", "paused"])
def test_tiktok_creator_is_lazy_and_cached_per_job(
    monkeypatch, settings, result, caplog
):
    from postpilot.video.tiktok import CreatorInfo, TikTokAPIError

    settings = replace(
        settings,
        tiktok=TikTokSettings(
            "sandbox",
            "key",
            "secret",
            "callback",
            mode="draft" if result == "draft" else "direct",
        ),
    )
    monkeypatch.setattr(
        tasks, "is_paused", lambda name: name == "tiktok" and result == "paused"
    )
    creator_info = CreatorInfo(["SELF_ONLY"], False, False, False, 90, "Creator")
    query = Mock(return_value=creator_info)
    if result == "error":
        query.side_effect = TikTokAPIError("private response body")
    monkeypatch.setattr(tasks.TikTokClient, "creator_info", query)
    access = Mock(return_value=None if result == "missing" else "private token")

    source = tasks.tiktok_creator_source(settings, Mock(), Mock(), access)
    access.assert_not_called()
    if result in {"draft", "paused"}:
        assert source is None
        query.assert_not_called()
        return
    expected = creator_info if result == "ok" else None
    assert source() == source() == expected
    access.assert_called_once_with()
    assert query.call_count == int(result != "missing")
    assert caplog.messages == (
        ["TikTok creator settings unavailable"] if expected is None else []
    )
