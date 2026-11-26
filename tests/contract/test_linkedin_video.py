import json
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from postpilot.channels.base import (
    ChannelError,
    TransientChannelError,
    UncertainPublication,
    marker,
)
from postpilot.channels.factory import FactoryLinkedInChannel
from postpilot.channels.linkedin import LinkedInChannel
from postpilot.channels.linkedin_video import LinkedInVideoChannel
from postpilot.core.models import Item, Variant
from postpilot.core.states import ItemState
from postpilot.notify.telegram import TelegramApi

URN = "urn:li:video:test"
POST_ID = "urn:li:share:1"
PNG = b"\x89PNG\r\n\x1a\ncover"
THUMBNAIL_URL = "https://upload.test/thumbnail?signed=private"
LABEL = "KI-generiertes Video / AI-generated video"


class VideoServer:
    def __init__(self):
        now = datetime.now(UTC)
        self.item = Item(uuid4(), "own_video", ItemState.APPROVED, now, now, 1, now)
        self.variants = [
            Variant(self.item.id, "de", "(Deutsch)"),
            Variant(self.item.id, "ua", "(Українська)"),
        ]
        self.data = b"a" * 4_000_000 + b"b" * 4_000_000 + b"c" * 1_000_000
        self.bounds = [(0, 3_999_999), (4_000_000, 7_999_999), (8_000_000, 8_999_999)]
        self.events = []
        self.sleeps = []
        self.fail_parts = 0
        self.fail_thumbnail = False
        self.factory = False
        self.thumbnail_url = THUMBNAIL_URL
        self.status = "AVAILABLE"
        self.posts = []
        self.lose_id = False

    def fetch(self, item_id):
        assert item_id == self.item.id
        self.events.append("fetch")
        return self.data

    def __call__(self, request):
        path = request.url.path
        if path == "/v2/userinfo":
            return httpx.Response(200, json={"sub": "person"})
        if request.method == "PUT":
            assert "authorization" not in request.headers
            assert request.headers["content-type"] == "application/octet-stream"
            if path == "/thumbnail":
                assert self.factory
                assert request.headers["media-type-family"] == "STILLIMAGE"
                assert request.content == PNG
                self.events.append("thumbnail")
                if self.fail_thumbnail:
                    self.fail_thumbnail = False
                    return httpx.Response(503)
                return httpx.Response(201)
            part = int(path.rsplit("/", 1)[-1])
            first, last = self.bounds[part]
            assert request.content == self.data[first : last + 1]
            self.events.append(f"part-{part}")
            if self.fail_parts:
                self.fail_parts -= 1
                return httpx.Response(500)
            return httpx.Response(200, headers={"ETag": f"etag-{part}"})

        assert request.headers["authorization"] == "Bearer token"
        assert request.headers["linkedin-version"] == "202611"
        assert request.headers["x-restli-protocol-version"] == "2.0.0"
        if path == "/rest/videos":
            action = request.url.params["action"]
            self.events.append(action)
            payload = json.loads(request.content)
            if action == "initializeUpload":
                assert payload == {
                    "initializeUploadRequest": {
                        "owner": "urn:li:person:person",
                        "fileSizeBytes": len(self.data),
                        "uploadCaptions": False,
                        "uploadThumbnail": self.factory,
                    }
                }
                return httpx.Response(
                    200,
                    json={
                        "value": {
                            "video": URN,
                            "uploadToken": "upload-token",
                            **(
                                {"thumbnailUploadUrl": self.thumbnail_url}
                                if self.factory and self.thumbnail_url is not None
                                else {}
                            ),
                            "uploadInstructions": [
                                {
                                    "uploadUrl": f"https://upload.test/{i}?signed=private",
                                    "firstByte": first,
                                    "lastByte": last,
                                }
                                for i, (first, last) in enumerate(self.bounds)
                            ],
                        }
                    },
                )
            assert action == "finalizeUpload"
            assert payload == {
                "finalizeUploadRequest": {
                    "video": URN,
                    "uploadToken": "upload-token",
                    "uploadedPartIds": ["etag-0", "etag-1", "etag-2"],
                }
            }
            return httpx.Response(200)
        if request.url.raw_path == b"/rest/videos/urn%3Ali%3Avideo%3Atest":
            self.events.append("poll")
            return httpx.Response(200, json={"status": self.status})
        assert path == "/rest/posts"
        if request.method == "GET":
            self.events.append("find")
            return httpx.Response(200, json={"elements": self.posts})
        self.events.append("post")
        self.posts.append({"id": POST_ID, **json.loads(request.content)})
        return httpx.Response(
            201, headers={} if self.lose_id else {"x-restli-id": POST_ID}
        )

    def publish(self, *, ref=None, ai=False, lang="de", fetch=None):
        name = "linkedin" if lang == "de" else "linkedin-ua"
        with httpx.Client(transport=httpx.MockTransport(self)) as client:
            channel = LinkedInVideoChannel(
                LinkedInChannel(name, lang, lambda: "token", client, "202611"),
                fetch_video=fetch or self.fetch,
                ai_generated=lambda _: ai,
                sleep=self.sleeps.append,
            )
            return channel.publish_with_ref(
                self.item,
                self.variants,
                ref=ref,
                claimed_at=self.item.updated_at,
                save_ref=self.events.append,
            )


@pytest.mark.parametrize(
    ("ai", "lang", "text"), [(False, "de", "Deutsch"), (True, "ua", "Українська")]
)
def test_multipart_upload_and_commentary(ai, lang, text, caplog):
    server = VideoServer()
    with caplog.at_level("INFO"):
        assert server.publish(ai=ai, lang=lang) == POST_ID
    assert "signed=private" not in caplog.text
    assert server.events == [
        "fetch",
        "initializeUpload",
        f"video:{URN}",
        "part-0",
        "part-1",
        "part-2",
        "finalizeUpload",
        f"video-final:{URN}",
        "poll",
        f"video-posting:{URN}",
        "post",
    ]
    post = server.posts[0]
    assert post["content"] == {"media": {"id": URN}}
    label = f"\n\n{LABEL}" if ai else ""
    assert (
        post["commentary"]
        == rf"\({text}\)" + label + f"\n\n{marker(server.item.id)}/{lang}"
    )


def test_label_counts_toward_length_limit():
    server = VideoServer()
    server.variants = [Variant(server.item.id, "de", "a" * 2949)]
    with pytest.raises(ChannelError, match="length limit"):
        server.publish(ai=True)
    assert server.events == []


@pytest.mark.parametrize("fail_parts", [1, 3])
def test_part_retry(fail_parts):
    server = VideoServer()
    server.fail_parts = fail_parts
    if fail_parts == 1:
        assert server.publish() == POST_ID
        assert server.sleeps == [1]
        assert server.events.count("part-0") == 2
    else:
        with pytest.raises(TransientChannelError):
            server.publish()
        assert server.sleeps == [1, 2]
        assert server.events.count("part-0") == 3
        assert "finalizeUpload" not in server.events


def test_processing_failed_is_permanent():
    server = VideoServer()
    server.status = "PROCESSING_FAILED"
    with pytest.raises(ChannelError, match="processing failed") as error:
        server.publish()
    assert not isinstance(error.value, TransientChannelError)
    assert not server.posts


def test_processing_is_bounded_then_resumes_without_upload():
    server = VideoServer()
    server.status = "PROCESSING"
    assert server.publish() is None
    assert server.events.count("poll") == 6
    assert server.sleeps == [5] * 5
    assert not server.posts

    server.events.clear()
    server.status = "AVAILABLE"
    assert server.publish(ref=f"video-final:{URN}") == POST_ID
    assert server.events == ["poll", "find", f"video-posting:{URN}", "post"]


def test_finalized_ref_recovers_existing_marker():
    server = VideoServer()
    server.posts = [
        {"id": POST_ID, "commentary": f"Text\n\n{marker(server.item.id)}/de"}
    ]
    assert server.publish(ref=f"video-final:{URN}") == POST_ID
    assert server.events == ["poll", "find"]


def test_unfinalized_ref_starts_a_fresh_upload():
    server = VideoServer()
    assert server.publish(ref="video:urn:li:video:interrupted") == POST_ID
    assert server.events[:3] == ["fetch", "initializeUpload", f"video:{URN}"]


def test_ambiguous_post_waits_for_marker_visibility():
    server = VideoServer()
    server.lose_id = True
    with pytest.raises(UncertainPublication):
        server.publish()
    post = server.posts.pop()
    start = len(server.events)
    assert server.publish(ref=f"video-posting:{URN}") is None
    server.posts.append(post)
    assert server.publish(ref=f"video-posting:{URN}") == POST_ID
    assert server.events[start:] == ["find", "find"]


@pytest.mark.parametrize("failure_method", ["POST", "GET"])
def test_telegram_fetch_error_is_permanent(failure_method):
    server = VideoServer()

    def telegram_response(request):
        if request.method == failure_method:
            return httpx.Response(403)
        assert request.url.path.endswith("/getFile")
        return httpx.Response(
            200, json={"ok": True, "result": {"file_path": "video.mp4"}}
        )

    with httpx.Client(transport=httpx.MockTransport(telegram_response)) as client:
        api = TelegramApi("telegram-token", client=client)
        with pytest.raises(
            ChannelError, match="Telegram video retrieval failed"
        ) as error:
            server.publish(fetch=lambda _: api.download_file(api.get_file("file-id")))
    assert not isinstance(error.value, TransientChannelError)
    assert server.events == []


@pytest.mark.parametrize("mode", ["success", "missing-url", "retry"])
def test_factory_thumbnail_upload(mode, caplog):
    server = VideoServer()
    server.factory = True
    server.fail_thumbnail = mode == "retry"
    if mode == "missing-url":
        server.thumbnail_url = None
    assets = Mock()
    assets.reel.return_value.post_de = "Deutsch"
    assets.reel.return_value.ai = False
    assets.video.side_effect = server.fetch
    assets.cover.return_value = PNG
    with httpx.Client(
        transport=httpx.MockTransport(server),
        headers={"Authorization": "default-token"},
    ) as client:
        channel = FactoryLinkedInChannel(
            LinkedInChannel("linkedin", "de", lambda: "token", client, "202611"), assets
        )
        channel._sleep = server.sleeps.append
        with caplog.at_level("INFO", logger="httpx"):

            def publish():
                return channel.publish_with_ref(
                    server.item,
                    [],
                    ref=None,
                    claimed_at=server.item.updated_at,
                    save_ref=server.events.append,
                )

            if mode == "missing-url":
                with pytest.raises(
                    ChannelError, match="did not return a thumbnail URL"
                ):
                    publish()
                assert "part-0" not in server.events
                assert "finalizeUpload" not in server.events
                return
            assert publish() == POST_ID
    assets.cover.assert_called_once_with(server.item.id)
    start = server.events.index("part-0")
    end = server.events.index("finalizeUpload")
    attempts = 2 if mode == "retry" else 1
    assert (
        server.events[start:end]
        == ["part-0", "part-1", "part-2"] + ["thumbnail"] * attempts
    )
    assert server.sleeps == ([1.0] if mode == "retry" else [])
    assert THUMBNAIL_URL not in caplog.text
