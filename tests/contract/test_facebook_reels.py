import logging
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest

from postpilot.channels.base import (
    ChannelError,
    ChannelHeld,
    TransientChannelError,
    UncertainPublication,
)
from postpilot.channels.facebook_reels import FacebookReelsChannel
from postpilot.config import FacebookSettings
from postpilot.tokens.facebook import token_is_valid
from postpilot.video.store import VideoRender, VideoStatus

TOKEN = "example-test-token"
SETTINGS = FacebookSettings("123")


@pytest.fixture
def render():
    return VideoRender(
        uuid4(),
        uuid4(),
        "de",
        "rendered",
        VideoStatus.APPROVED,
        "Ein Skript.",
        b"video",
        25,
        datetime.now(UTC),
        1,
        datetime.now(UTC),
    )


def run(render, route, *, ref=None, save=lambda ref: None):
    def handle(request):
        assert request.headers["Authorization"] == f"OAuth {TOKEN}"
        assert TOKEN not in str(request.url)
        assert TOKEN.encode() not in request.content
        return route(request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        return FacebookReelsChannel(SETTINGS, client).publish_with_ref(
            render, TOKEN, ref, save
        )


def test_start_checkpoints_before_upload_and_publishes_labelled_caption(render):
    calls = []

    def route(request):
        calls.append(request.url.path)
        if request.url.host == "rupload.facebook.com":
            assert calls[-2] == "saved:456"
            assert request.url.path == "/video-upload/v27.0/456"
            assert request.headers["offset"] == "0"
            assert request.headers["file_size"] == "5"
            assert request.headers["content-type"] == "application/octet-stream"
            assert request.content == b"video"
            return httpx.Response(200, json={"success": True})
        assert request.url.host == "graph.facebook.com"
        assert request.url.path == "/v27.0/123/video_reels"
        assert request.method == "POST"
        form = parse_qs(request.content.decode())
        if form == {"upload_phase": ["start"]}:
            return httpx.Response(200, json={"video_id": "456"})
        assert form == {
            "upload_phase": ["finish"],
            "video_id": ["456"],
            "video_state": ["PUBLISHED"],
            "description": [
                "Ein Skript.\n\nKI-generierter Avatar / AI-generated avatar"
            ],
        }
        return httpx.Response(200, json={"success": True})

    assert run(render, route, save=lambda ref: calls.append(f"saved:{ref}")) == "456"
    assert len(calls) == 4


@pytest.mark.parametrize(
    "status,offset,steps,result",
    [
        ({"publishing_phase": {"status": "complete"}}, None, [], "old"),
        (
            {"uploading_phase": {"status": "in_progress", "bytes_transfered": 2}},
            2,
            ["upload", "finish"],
            "old",
        ),
        ({}, 0, ["upload", "finish"], "old"),
        (
            {
                "uploading_phase": {"status": "complete"},
                "publishing_phase": {"status": "not_started"},
            },
            None,
            ["finish"],
            "old",
        ),
        (
            {
                "uploading_phase": {"status": "complete"},
                "processing_phase": {"status": "in_progress"},
            },
            None,
            [],
            None,
        ),
        ({"publishing_phase": {"status": "in_progress"}}, None, [], None),
        (
            {"uploading_phase": {"status": "error"}},
            0,
            ["start", "upload", "finish"],
            "new",
        ),
        (
            {"processing_phase": {"error": {"message": "secret"}}},
            0,
            ["start", "upload", "finish"],
            "new",
        ),
        (
            {"publishing_phase": {"errors": [{"message": "secret"}]}},
            0,
            ["start", "upload", "finish"],
            "new",
        ),
    ],
)
def test_recovery(render, status, offset, steps, result):
    calls = []
    saved = []

    def route(request):
        if request.method == "GET":
            assert not calls
            assert request.url.path == "/v27.0/old"
            assert dict(request.url.params) == {"fields": "status"}
            calls.append("lookup")
            return httpx.Response(200, json={"status": status})
        if request.url.host == "rupload.facebook.com":
            calls.append("upload")
            assert request.headers["offset"] == str(offset)
            assert request.headers["file_size"] == "5"
            assert request.headers["content-type"] == "application/octet-stream"
            assert request.content == render.video[offset:]
        else:
            form = parse_qs(request.content.decode())
            step = form["upload_phase"][0]
            calls.append(step)
            if step == "start":
                return httpx.Response(200, json={"video_id": "new"})
            assert form["video_id"] == [result]
        return httpx.Response(200, json={"success": True})

    assert run(render, route, ref="old", save=saved.append) == result
    assert calls == ["lookup", *steps]
    assert saved == (["new"] if "start" in steps else [])


@pytest.mark.parametrize(
    "phase,status,code,expected",
    [
        ("finish", 500, None, UncertainPublication),
        ("finish", None, None, UncertainPublication),
        ("start", 503, None, TransientChannelError),
        ("start", None, None, TransientChannelError),
        ("start", 400, None, ChannelError),
        ("finish", 403, None, ChannelError),
        ("finish", 400, 190, ChannelHeld),
    ],
)
def test_failures_are_classified_and_redacted(render, phase, status, code, expected):
    def route(request):
        if f"upload_phase={phase}".encode() in request.content:
            if status is None:
                raise httpx.ReadTimeout(TOKEN, request=request)
            return httpx.Response(
                status, json={"error": {"code": code, "message": TOKEN}}
            )
        return httpx.Response(200, json={"video_id": "id", "success": True})

    with pytest.raises(expected) as caught:
        run(render, route)
    assert type(caught.value) is expected
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "changed",
    [
        {"status": VideoStatus.READY},
        {"status": VideoStatus.REJECTED},
        {"video": None},
        {"video": b""},
        {"script": None},
        {"script": " "},
        {"duration": 2.99},
        {"duration": 90.01},
    ],
)
def test_validation_precedes_all_requests(render, changed):
    with pytest.raises(ChannelError):
        run(replace(render, **changed), lambda _: pytest.fail("unexpected request"))


@pytest.mark.parametrize("duration", [None, 3, 90])
def test_duration_boundaries(render, duration):
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail())) as client:
        FacebookReelsChannel(SETTINGS, client).validate(
            replace(render, duration=duration)
        )


@pytest.mark.parametrize(
    "status,payload,valid",
    [
        (200, {"data": {"is_valid": True}}, True),
        (200, {"data": {"is_valid": False}}, False),
        (400, {"error": {"code": 190}}, False),
    ],
)
def test_debug_token_request_redacts_url(caplog, status, payload, valid):
    def route(request):
        assert request.url.path == "/v27.0/debug_token"
        assert request.url.params["input_token"] == TOKEN
        assert request.headers["Authorization"] == f"OAuth {TOKEN}"
        return httpx.Response(status, json=payload)

    with caplog.at_level(logging.INFO, logger="httpx"):
        with httpx.Client(transport=httpx.MockTransport(route)) as client:
            assert token_is_valid(SETTINGS, client, TOKEN) is valid
        logging.getLogger("httpx").info("ordinary request")
    assert "ordinary request" in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.parametrize(
    "phase,expected",
    [
        ("start", TransientChannelError),
        ("finish", UncertainPublication),
    ],
)
@pytest.mark.parametrize(
    "status,error",
    [
        *[(400, {"code": code}) for code in (1, 2, 4, 17, 32, 341, 613)],
        (400, {"code": 999, "is_transient": True}),
        (429, {}),
        (400, {"code": 190, "is_transient": True}),
    ],
)
def test_graph_retryable_errors_and_invalid_token_precedence(
    render, phase, expected, status, error
):
    def route(request):
        if f"upload_phase={phase}".encode() in request.content:
            return httpx.Response(status, json={"error": {**error, "message": TOKEN}})
        return httpx.Response(200, json={"video_id": "id", "success": True})

    expected = ChannelHeld if error.get("code") == 190 else expected
    with pytest.raises(expected) as caught:
        run(render, route)
    assert type(caught.value) is expected
    assert TOKEN not in str(caught.value)
