from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest

from postpilot.channels.base import (
    ChannelError,
    ChannelHeld,
    TransientChannelError,
    UncertainPublication,
)
from postpilot.channels.instagram_reels import InstagramReelsChannel
from postpilot.config import InstagramSettings
from postpilot.video.reel import ReelVideo

NOW = datetime(2026, 11, 20, tzinfo=UTC)
TOKEN = "test-instagram-token"
CAPTION = "Ein Skript.\n\nKI-generierter Avatar / AI-generated avatar"
LINK = "https://www.instagram.com/reel/example/"


@pytest.fixture
def render():
    return ReelVideo(
        b"video", "Ein Skript.\n\nKI-generierter Avatar / AI-generated avatar", 25, True
    )


class Scenario:
    def __init__(self, render, replies):
        self.render = render
        self.replies = iter(replies)
        self.calls = []
        self.elapsed = 0

    def sleep(self, seconds):
        self.elapsed += seconds

    def handle(self, request):
        assert request.headers["Authorization"] == f"OAuth {TOKEN}"
        assert TOKEN not in str(request.url)
        path, reply = next(self.replies)
        assert request.url.path == path
        self.calls.append(path)
        if request.url.host == "rupload.facebook.com":
            assert self.calls[-2] == "saved:new"
            assert request.method == "POST"
            assert request.headers["offset"] == "0"
            assert request.headers["file_size"] == "5"
            assert request.content == b"video"
        elif path == "/v27.0/123/media" and request.method == "POST":
            assert parse_qs(request.content.decode()) == {
                "media_type": ["REELS"],
                "upload_type": ["resumable"],
                "caption": [CAPTION],
                "is_ai_generated": ["true"],
            }
        elif path.endswith("/media_publish"):
            assert request.method == "POST"
            assert parse_qs(request.content.decode()) == {
                "creation_id": [self.calls[-2].removeprefix("saved:")]
            }
        else:
            assert request.method == "GET"
            fields = request.url.params["fields"]
            if path.endswith("/media"):
                assert fields == "id,caption,timestamp"
                if request.url.params.get("after") is not None:
                    assert request.url.params["after"] == "cursor"
            elif path.endswith("/media-id"):
                assert fields == "permalink"
            else:
                assert fields == "status_code,status"
        if reply is None:
            raise httpx.ReadTimeout(TOKEN, request=request)
        status, payload = reply if isinstance(reply, tuple) else (200, reply)
        return httpx.Response(status, json=payload)

    def run(self, ref=None, *, age=0):
        with httpx.Client(transport=httpx.MockTransport(self.handle)) as client:
            channel = InstagramReelsChannel(
                InstagramSettings("123"),
                client,
                sleep=self.sleep,
                clock=lambda: NOW + timedelta(seconds=self.elapsed),
            )
            result = channel.publish_with_ref(
                self.render,
                TOKEN,
                ref,
                lambda value: self.calls.append(f"saved:{value}"),
                claimed_at=NOW - timedelta(seconds=age),
            )
            assert next(self.replies, None) is None
            return result, channel.permalink(result) if result else None


CREATE_UPLOAD = [
    ("/v27.0/123/media", {"id": "new"}),
    ("/ig-api-upload/v27.0/new", {"success": True}),
]
PUBLISH = ("/v27.0/123/media_publish", {"id": "media-id"})
PERMALINK = ("/v27.0/media-id", {"permalink": LINK})


def status(ref, code):
    return f"/v27.0/{ref}", {"status_code": code}


def test_create_upload_poll_publish_and_cache_link(render):
    scenario = Scenario(
        render,
        [
            *CREATE_UPLOAD,
            status("new", "IN_PROGRESS"),
            status("new", "FINISHED"),
            PUBLISH,
            PERMALINK,
        ],
    )
    assert scenario.run() == ("media-id", LINK)
    assert scenario.elapsed == 10


def test_finished_republishes_same_container_and_link_is_best_effort(render):
    scenario = Scenario(
        render,
        [
            status("old", "FINISHED"),
            PUBLISH,
            ("/v27.0/media-id", (400, {"error": {"code": 190}})),
        ],
    )
    assert scenario.run("old") == ("media-id", "instagram media media-id")
    assert "saved:old" in scenario.calls


def post(remote_id, *, caption=CAPTION, minutes=0):
    return {
        "id": remote_id,
        "caption": caption,
        "timestamp": (NOW + timedelta(minutes=minutes)).isoformat(),
    }


def test_published_recovery_uses_cursor_and_exact_recent_caption(render):
    scenario = Scenario(
        render,
        [
            status("old", "PUBLISHED"),
            (
                "/v27.0/123/media",
                {
                    "data": [
                        post("too-old", minutes=-6),
                        post("different", caption=CAPTION + " "),
                    ],
                    "paging": {
                        "next": f"https://graph.facebook.com/v27.0/123/media?after=cursor&access_token={TOKEN}"
                    },
                },
            ),
            ("/v27.0/123/media", {"data": [post("media-id", minutes=-5)]}),
            PERMALINK,
        ],
    )
    assert scenario.run("old") == ("media-id", LINK)


@pytest.mark.parametrize("posts", [[], [post("one"), post("two")]])
def test_unmatched_or_ambiguous_published_is_uncertain(render, posts):
    with pytest.raises(UncertainPublication):
        Scenario(
            render,
            [
                status("old", "PUBLISHED"),
                ("/v27.0/123/media", {"data": posts}),
            ],
        ).run("old")


def test_in_progress_polls_until_pending(render):
    scenario = Scenario(render, [status("old", "IN_PROGRESS")] * 31)
    assert scenario.run("old") == (None, None)
    assert scenario.elapsed == 300


@pytest.mark.parametrize(
    "code,age", [("IN_PROGRESS", 3601), ("ERROR", 0), ("EXPIRED", 0)]
)
@pytest.mark.parametrize("new_status", ["FINISHED", "ERROR", "EXPIRED"])
def test_abandoned_container_replaced_once(render, code, age, new_status):
    replies = [status("old", code), *CREATE_UPLOAD, status("new", new_status)]
    if new_status == "FINISHED":
        replies += [PUBLISH, PERMALINK]
    assert Scenario(render, replies).run("old", age=age) == (
        ("media-id", LINK) if new_status == "FINISHED" else (None, None)
    )


@pytest.mark.parametrize(
    "replies,expected",
    [
        (
            [("/v27.0/123/media", (400, {"error": {"code": 190, "message": TOKEN}}))],
            ChannelHeld,
        ),
        ([("/v27.0/123/media", (503, {}))], TransientChannelError),
        ([("/v27.0/123/media", (429, {}))], TransientChannelError),
        ([("/v27.0/123/media", (400, {"error": {"code": 4}}))], TransientChannelError),
        ([("/v27.0/123/media", (400, {}))], ChannelError),
        (
            [CREATE_UPLOAD[0], ("/ig-api-upload/v27.0/new", {"success": False})],
            TransientChannelError,
        ),
        (
            [status("old", "FINISHED"), ("/v27.0/123/media_publish", None)],
            UncertainPublication,
        ),
        (
            [status("old", "FINISHED"), ("/v27.0/123/media_publish", (503, {}))],
            UncertainPublication,
        ),
    ],
)
def test_failures(render, replies, expected):
    with pytest.raises(expected) as caught:
        Scenario(render, replies).run(
            "old"
            if len(replies) == 2 and replies[1][0].endswith("/media_publish")
            else None
        )
    assert type(caught.value) is expected
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "changed",
    [
        {"video": b""},
        {"caption": " "},
        {"duration": 900.01},
        {"duration": 2.99},
    ],
)
def test_validation_precedes_requests(render, changed):
    with pytest.raises(ChannelError):
        Scenario(replace(render, **changed), []).run()
