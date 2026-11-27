import json
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import httpx
import pytest
from test_tiktok_upload import COMPLETE, INBOX, NOW, PROCESSING, UPLOAD_URL, response

from postpilot.channels.base import (
    ChannelError,
    ChannelHeld,
    ChannelSkipped,
    UncertainPublication,
)
from postpilot.channels.tiktok import TikTokChannel
from postpilot.core.tiktok import TikTokChoice
from postpilot.notify.telegram import Notifier
from postpilot.video.label import AI_VIDEO_CAPTION_LABEL
from postpilot.video.reel import ReelVideo
from postpilot.video.tiktok import CreatorInfo, TikTokAPIError, TikTokClient

CREATOR = {
    "privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"],
    "comment_disabled": False,
    "duet_disabled": False,
    "stitch_disabled": False,
    "max_video_post_duration_sec": 60,
    "creator_nickname": "Creator",
}
REEL = ReelVideo(b"video", "Beschreibung", 60, False, tiktok=TikTokChoice("SELF_ONLY"))


@pytest.mark.parametrize(
    "override",
    [
        {},
        {"privacy_level_options": "SELF_ONLY"},
        {"privacy_level_options": [1]},
        {"comment_disabled": 0},
        {"duet_disabled": None},
        {"stitch_disabled": "false"},
        {"max_video_post_duration_sec": True},
        {"max_video_post_duration_sec": 0},
        {"max_video_post_duration_sec": 60.5},
        {"creator_nickname": None},
    ],
)
def test_creator_info(override):
    def route(request):
        assert request.method == "POST"
        assert request.url.path == "/v2/post/publish/creator_info/query/"
        assert json.loads(request.content) == {}
        return response(**(CREATOR | override))

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        api = TikTokClient(client, "access")
        if override:
            with pytest.raises(TikTokAPIError, match="malformed creator info"):
                api.creator_info()
        else:
            assert api.creator_info() == CreatorInfo(**CREATOR)


@pytest.mark.parametrize("ai", [False, True])
@pytest.mark.parametrize(
    ("allows", "creator_disables", "disables"),
    [
        ((False, False, False), (False, False, False), (True, True, True)),
        ((True, True, True), (False, False, False), (False, False, False)),
        ((True, True, True), (True, True, True), (True, True, True)),
        ((True, False, True), (False, False, True), (False, True, True)),
    ],
)
def test_direct_payload(ai, allows, creator_disables, disables):
    label = f"\n\n{AI_VIDEO_CAPTION_LABEL}" if ai else ""
    caption = "ä" * (2200 - len(label)) + label
    reel = replace(
        REEL,
        caption=caption,
        ai_generated=ai,
        tiktok=TikTokChoice("SELF_ONLY", *allows),
    )
    save_ref = Mock()
    requests = []

    def route(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/query/"):
            return response(
                **(
                    CREATOR
                    | dict(
                        zip(
                            ("comment_disabled", "duet_disabled", "stitch_disabled"),
                            creator_disables,
                            strict=True,
                        )
                    )
                )
            )
        if request.url.path.endswith("/init/"):
            assert request.method == "POST"
            assert json.loads(request.content) == {
                "post_info": {
                    "title": caption,
                    "is_aigc": ai,
                    "video_cover_timestamp_ms": 0,
                    "brand_content_toggle": False,
                    "brand_organic_toggle": False,
                    "privacy_level": "SELF_ONLY",
                    "disable_comment": disables[0],
                    "disable_duet": disables[1],
                    "disable_stitch": disables[2],
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": 5,
                    "chunk_size": 5,
                    "total_chunk_count": 1,
                },
            }
            assert json.loads(request.content)["post_info"]["is_aigc"] is ai
            return response(publish_id="new", upload_url=UPLOAD_URL)
        if request.method == "PUT":
            save_ref.assert_called_once_with("new")
            assert request.headers["Content-Range"] == "bytes 0-4/5"
            assert request.content == reel.video
            return httpx.Response(201)
        assert json.loads(request.content) == {"publish_id": "new"}
        return response(status=COMPLETE)

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        channel = TikTokChannel(client, mode="direct")
        assert (
            channel.publish_with_ref(reel, "access", None, save_ref, claimed_at=NOW)
            == "new"
        )
        assert channel.permalink("new") == ""
        assert channel.caption("new") is None
    assert requests == [
        "/v2/post/publish/creator_info/query/",
        "/v2/post/publish/video/init/",
        "/send",
        "/v2/post/publish/status/fetch/",
    ]


@pytest.mark.parametrize(
    ("changes", "error", "message", "queried"),
    [
        ({"caption": "ä" * 2201}, ChannelSkipped, "2200-character limit", False),
        ({"tiktok": None}, ChannelHeld, None, False),
        (
            {"tiktok": TikTokChoice("FRIENDS")},
            ChannelError,
            "privacy level is no longer offered",
            True,
        ),
        ({"duration": 60.01}, ChannelError, "to 60 seconds", True),
    ],
)
def test_direct_rejection_before_upload(changes, error, message, queried):
    def route(request):
        assert request.url.path.endswith("/query/")
        return response(**CREATOR)

    transport = Mock(side_effect=route)
    load = Mock()
    save_ref = Mock()
    with (
        httpx.Client(transport=httpx.MockTransport(transport)) as client,
        pytest.raises(error, match=message),
    ):
        TikTokChannel(client, mode="direct").publish_with_ref(
            replace(REEL, video=load, **changes),
            "access",
            None,
            save_ref,
            claimed_at=NOW,
        )
    assert transport.call_count == int(queried)
    load.assert_not_called()
    save_ref.assert_not_called()


@pytest.mark.parametrize("ref", [None, "old"])
def test_project_without_choice_goes_to_inbox_and_caption_is_available(ref):
    reel = replace(REEL, tiktok=None, inbox_fallback=True)
    paths = []

    def route(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/init/"):
            assert "post_info" not in json.loads(request.content)
            return response(publish_id="new", upload_url=UPLOAD_URL)
        if request.method == "PUT":
            return httpx.Response(201)
        return response(status=INBOX)

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        channel = TikTokChannel(client, mode="direct")
        with pytest.raises(ChannelHeld):
            channel.publish_with_ref(
                replace(reel, inbox_fallback=False),
                "access",
                None,
                Mock(),
                claimed_at=NOW,
            )
        published = channel.publish_with_ref(
            reel, "access", ref, Mock(), claimed_at=NOW
        )
        assert published == (ref or "new")
        api = Mock()
        Notifier(api, 1).send_tiktok_draft(channel.caption(published), fallback=True)
        api.send_message.assert_called_once_with(
            1,
            "tiktok: no TikTok choice by release time, sent to your TikTok inbox. "
            "Finish in the TikTok app.\nCaption to copy:\nBeschreibung",
        )
    assert paths == (
        ["/v2/post/publish/status/fetch/"]
        if ref
        else [
            "/v2/post/publish/inbox/video/init/",
            "/send",
            "/v2/post/publish/status/fetch/",
        ]
    )


@pytest.mark.parametrize(
    ("status", "age", "expected"),
    [
        (COMPLETE, timedelta(hours=2), "old"),
        (INBOX, timedelta(), None),
        (PROCESSING, timedelta(hours=1), None),
        ("PROCESSING_DOWNLOAD", timedelta(minutes=20), None),
        (PROCESSING, timedelta(hours=1, seconds=1), "new"),
        ("FAILED", timedelta(), "new"),
    ],
)
def test_direct_recovery(status, age, expected):
    def route(request):
        if request.url.path.endswith("/fetch/"):
            ref = json.loads(request.content)["publish_id"]
            return response(status=status if ref == "old" else COMPLETE)
        if request.url.path.endswith("/query/"):
            return response(**CREATOR)
        if request.method == "PUT":
            save_ref.assert_called_once_with("new")
            return httpx.Response(201)
        assert request.url.path == "/v2/post/publish/video/init/"
        return response(publish_id="new", upload_url=UPLOAD_URL)

    transport = Mock(side_effect=route)
    load = Mock(return_value=b"video")
    save_ref = Mock()
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        channel = TikTokChannel(client, mode="direct", clock=lambda: NOW)
        assert (
            channel.publish_with_ref(
                replace(REEL, video=load),
                "access",
                "old",
                save_ref,
                claimed_at=NOW - age,
            )
            == expected
        )
        assert (
            channel.publish_with_ref(
                REEL, "access", "another", save_ref, claimed_at=NOW
            )
            is None
        )
    if expected == "new":
        assert transport.call_count == 5
        load.assert_called_once_with()
    else:
        assert transport.call_count == 1
        load.assert_not_called()
        save_ref.assert_not_called()


@pytest.mark.parametrize(
    "code",
    [
        "rate_limit_exceeded",
        "spam_risk_too_many_pending_share",
        "spam_risk_too_many_posts",
    ],
)
def test_direct_rate_and_spam_errors_remain_pending(code):
    def route(request):
        if request.url.path.endswith("/query/"):
            return response(**CREATOR)
        return httpx.Response(429, json={"error": {"code": code}})

    with (
        httpx.Client(transport=httpx.MockTransport(route)) as client,
        pytest.raises(UncertainPublication, match=code),
    ):
        TikTokChannel(client, mode="direct").publish_with_ref(
            REEL, "access", None, Mock(), claimed_at=NOW
        )


@pytest.mark.parametrize(
    ("outcome", "caption", "expected"),
    [
        ("published", None, "tiktok: posted"),
        (
            "published",
            "Beschreibung",
            (
                "tiktok: no TikTok choice by release time, sent to your TikTok inbox. "
                "Finish in the TikTok app.\nCaption to copy:\nBeschreibung"
            ),
        ),
        (
            "skipped",
            None,
            "tiktok: skipped, caption exceeds TikTok's 2200-character limit.",
        ),
    ],
)
def test_direct_telegram_result(monkeypatch, outcome, caption, expected):
    from types import SimpleNamespace
    from uuid import uuid4

    from postpilot.config import Settings, TikTokSettings
    from postpilot.core.publisher import ChannelResult
    from postpilot.jobs import tasks

    item_id = uuid4()
    api = Mock()
    repo = Mock()
    repo.list_approved_item_ids.return_value = [item_id]
    repo.list_unfinished_item_ids.return_value = []
    repo.list_publications.return_value = [
        SimpleNamespace(channel="tiktok", remote_id="published-id")
    ]
    monkeypatch.setattr(tasks, "ItemRepo", lambda _: repo)
    monkeypatch.setattr(tasks, "TelegramApi", Mock(return_value=api))
    monkeypatch.setattr(tasks, "TokenCipher", Mock())
    monkeypatch.setattr(tasks, "publish_one", Mock())
    monkeypatch.setattr(tasks, "scan_projects", Mock())
    monkeypatch.setattr(tasks.Projects, "load", list)
    monkeypatch.setattr(tasks.ProjectItemStore, "list_due_item_ids", lambda *_: [])
    monkeypatch.setattr(tasks, "is_paused", lambda _: False)

    def publish_items(publisher, _ids, *, on_result, notifier):
        channel = publisher.own_video_channels[0]._channel
        assert channel.mode == "direct"
        if caption is not None:
            channel._captions["published-id"] = caption
        error = (
            "caption exceeds TikTok's 2200-character limit"
            if outcome == "skipped"
            else None
        )
        on_result(item_id, "tiktok", ChannelResult(outcome, error))

    monkeypatch.setattr(tasks, "_publish_items", publish_items)
    settings = Settings(
        "db",
        "bot",
        1,
        "key",
        tiktok=TikTokSettings("sandbox", "id", "secret", "uri", mode="direct"),
    )
    tasks.publish_reels(settings, Mock())
    api.send_message.assert_called_once_with(1, expected)
