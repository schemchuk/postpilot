import json
from datetime import UTC, datetime
from unittest.mock import Mock

import httpx
import pytest

from postpilot.channels.base import ChannelError, ChannelHeld, UncertainPublication
from postpilot.channels.youtube import YouTubeChannel
from postpilot.video.label import AI_VIDEO_CAPTION_LABEL
from postpilot.video.reel import ReelVideo

NOW = datetime(2026, 11, 26, 23, 5, tzinfo=UTC)
SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=session"
DEFAULT_TITLE = "Volodymyr Shemchuk · 27.11.2026"
VIDEO = b"123456"


@pytest.mark.parametrize(
    "text, ai, title, expected_title",
    [
        ("<Kurzer Titel>\nZweite Zeile.", True, None, "‹Kurzer Titel›"),
        ("Erster Satz. " + "x" * 100, False, None, "Erster Satz."),
        ("x" * 101 + ".", False, None, DEFAULT_TITLE),
        ("x" * 100, False, None, "x" * 100),
        ("Beschreibung.", True, " <Eigener Titel> ", "‹Eigener Titel›"),
        ("Beschreibung.", False, "x" * 101, DEFAULT_TITLE),
        ("", True, None, DEFAULT_TITLE),
        ("", False, None, DEFAULT_TITLE),
    ],
)
def test_upload_payload_and_checkpoint(text, ai, title, expected_title):
    saved = []
    caption = "\n\n".join(
        part for part in (text, AI_VIDEO_CAPTION_LABEL if ai else "") if part
    )

    def route(request):
        assert request.headers["Authorization"] == "Bearer token"
        if request.method == "POST":
            assert dict(request.url.params) == {
                "uploadType": "resumable",
                "part": "snippet,status",
            }
            assert request.headers["X-Upload-Content-Length"] == "6"
            assert request.headers["X-Upload-Content-Type"] == "video/mp4"
            assert json.loads(request.content) == {
                "snippet": {
                    "title": expected_title,
                    "description": caption.replace("<", "‹").replace(">", "›"),
                    "categoryId": "28",
                },
                "status": {
                    "privacyStatus": "public",
                    "selfDeclaredMadeForKids": False,
                    "containsSyntheticMedia": ai,
                },
            }
            return httpx.Response(200, headers={"Location": SESSION})
        assert saved == [SESSION]
        assert str(request.url) == SESSION
        assert request.headers["Content-Range"] == "bytes 0-5/6"
        assert request.content == VIDEO
        return httpx.Response(201, json={"id": "video-id"})

    reel = ReelVideo(VIDEO, caption, 20, ai, title=title)
    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        channel = YouTubeChannel(client, clock=lambda: NOW)
        assert (
            channel.publish_with_ref(reel, "token", None, saved.append, claimed_at=NOW)
            == "video-id"
        )
        with pytest.raises(ChannelHeld):
            channel.publish_with_ref(reel, "token", None, saved.append, claimed_at=NOW)
    assert saved == [SESSION, "video-id"]


@pytest.mark.parametrize("status", [200, 201, 308, 404])
def test_recover_saved_session(status):
    calls = []
    saved = []

    def route(request):
        calls.append(request.method)
        if len(calls) == 1:
            assert request.method == "PUT"
            assert request.headers["Content-Range"] == "bytes */6"
            assert request.content == b""
            if status in (200, 201):
                return httpx.Response(status, json={"id": "video-id"})
            return httpx.Response(
                status, headers={"Range": "bytes=0-2"} if status == 308 else {}
            )
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": SESSION + "-new"})
        assert saved == [str(request.url)]
        assert request.headers["Content-Range"] == (
            "bytes 3-5/6" if status == 308 else "bytes 0-5/6"
        )
        assert request.content == (VIDEO[3:] if status == 308 else VIDEO)
        return httpx.Response(201, json={"id": "video-id"})

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        reel = ReelVideo(VIDEO, "Beschreibung", 20, False)
        assert (
            YouTubeChannel(client).publish_with_ref(
                reel, "token", SESSION, saved.append, claimed_at=NOW
            )
            == "video-id"
        )
        load = Mock(side_effect=AssertionError("A completed upload needs no video"))
        completed = ReelVideo(load, "", 20, False)
        assert (
            YouTubeChannel(client).publish_with_ref(
                completed, "token", saved[-1], saved.append, claimed_at=NOW
            )
            == "video-id"
        )
        load.assert_not_called()
    assert saved[-1] == "video-id"
    assert (
        calls
        == {
            200: ["PUT"],
            201: ["PUT"],
            308: ["PUT", "PUT"],
            404: ["PUT", "POST", "PUT"],
        }[status]
    )


@pytest.mark.parametrize("ref", [None, SESSION])
@pytest.mark.parametrize(
    "status, reason",
    [
        (429, ""),
        (403, "quotaExceeded"),
        (403, "rateLimitExceeded"),
        (403, "userRateLimitExceeded"),
    ],
)
def test_quota_pending_without_discarding_checkpoint(ref, status, reason):
    response = httpx.Response(status, json={"error": {"errors": [{"reason": reason}]}})
    route = Mock(return_value=response)
    save = Mock()
    reel = ReelVideo(VIDEO, "", 20, False)
    with (
        httpx.Client(transport=httpx.MockTransport(route)) as client,
        pytest.raises(UncertainPublication, match="quota or rate limit"),
    ):
        YouTubeChannel(client).publish_with_ref(
            reel, "token", ref, save, claimed_at=NOW
        )
    route.assert_called_once()
    save.assert_not_called()


def test_description_limit_counts_escaped_utf8_bytes():
    route = Mock(side_effect=AssertionError("Oversize description sent to YouTube"))
    reel = ReelVideo(Mock(), "<" * 1667, 20, False)
    with (
        httpx.Client(transport=httpx.MockTransport(route)) as client,
        pytest.raises(ChannelError, match="5000 UTF-8 bytes"),
    ):
        YouTubeChannel(client).publish_with_ref(
            reel, "token", None, Mock(), claimed_at=NOW
        )
    route.assert_not_called()
