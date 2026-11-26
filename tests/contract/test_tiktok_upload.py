import json
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, call

import httpx
import pytest

from postpilot.channels.base import ChannelError, ChannelHeld, UncertainPublication
from postpilot.channels.tiktok import TikTokChannel
from postpilot.video.reel import ReelVideo

NOW = datetime(2026, 11, 26, tzinfo=UTC)
UPLOAD_URL = "https://upload.example.test/send?signature=private"
INBOX = "SEND_TO_USER_INBOX"
COMPLETE = "PUBLISH_COMPLETE"
PROCESSING = "PROCESSING_UPLOAD"


def response(**data):
    return httpx.Response(200, json={"data": data, "error": {"code": "ok"}})


@pytest.mark.parametrize(("size", "chunk", "count"), [(8, 8, 1), (25, 10, 2)])
@pytest.mark.parametrize("done", [INBOX, COMPLETE])
def test_upload_saves_ref_before_chunks_and_polls(
    monkeypatch, caplog, size, chunk, count, done
):
    monkeypatch.setattr("postpilot.video.tiktok._CHUNK_SIZE", 10)
    monkeypatch.setattr("postpilot.video.tiktok._SINGLE_CHUNK_LIMIT", 20)
    save_ref = Mock()
    sleep = Mock()
    caption = "Beschreibung"
    video = bytes(range(size))
    chunks = []
    statuses = iter([PROCESSING, done])

    def route(request):
        if request.method == "PUT":
            save_ref.assert_called_once_with("new")
            assert str(request.url) == UPLOAD_URL
            assert "authorization" not in request.headers
            chunks.append((request.headers["Content-Range"], request.content))
            return httpx.Response(201)
        body = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer access"
        if request.url.path.endswith("/init/"):
            assert body == {
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": chunk,
                    "total_chunk_count": count,
                }
            }
            return response(publish_id="new", upload_url=UPLOAD_URL)
        assert len(chunks) == count
        assert body == {"publish_id": "new"}
        return response(status=next(statuses))

    reel = ReelVideo(video, caption, 10, False)
    with (
        httpx.Client(transport=httpx.MockTransport(route)) as client,
        caplog.at_level(logging.DEBUG),
    ):
        channel = TikTokChannel(client, sleep=sleep)
        assert (
            channel.publish_with_ref(reel, "access", None, save_ref, claimed_at=NOW)
            == "new"
        )
        assert channel.caption("new") == caption
        with pytest.raises(ChannelHeld):
            channel.publish_with_ref(reel, "access", "other", save_ref, claimed_at=NOW)
    assert chunks == (
        [("bytes 0-7/8", video)]
        if count == 1
        else [("bytes 0-9/25", video[:10]), ("bytes 10-24/25", video[10:])]
    )
    sleep.assert_called_once_with(10)
    assert "signature=private" not in caplog.text


@pytest.mark.parametrize(
    ("status", "age", "expected"),
    [
        (INBOX, timedelta(hours=2), "old"),
        (COMPLETE, timedelta(), "old"),
        (PROCESSING, timedelta(hours=1), None),
        (PROCESSING, timedelta(hours=1, seconds=1), "new"),
        ("FAILED", timedelta(), "new"),
    ],
)
def test_recovery(status, age, expected):
    paths = []
    load_video = Mock(return_value=b"video")
    save_ref = Mock()

    def route(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/init/"):
            return response(publish_id="new", upload_url=UPLOAD_URL)
        if request.method == "PUT":
            return httpx.Response(201)
        body = json.loads(request.content)
        return response(status=status if body["publish_id"] == "old" else INBOX)

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        channel = TikTokChannel(client, clock=lambda: NOW)
        assert (
            channel.publish_with_ref(
                ReelVideo(load_video, "Caption", 10, False),
                "access",
                "old",
                save_ref,
                claimed_at=NOW - age,
            )
            == expected
        )
        if expected is not None:
            assert channel.caption(expected) == "Caption"
    assert paths[0].endswith("/status/fetch/")
    if expected == "new":
        assert [path.rstrip("/").rsplit("/", 1)[-1] for path in paths] == [
            "fetch",
            "init",
            "send",
            "fetch",
        ]
        load_video.assert_called_once_with()
        save_ref.assert_called_once_with("new")
    else:
        assert len(paths) == 1
        load_video.assert_not_called()
        save_ref.assert_not_called()


@pytest.mark.parametrize(
    ("status", "reason", "error_text"),
    [
        (PROCESSING, None, None),
        ("FAILED", "file_format_check_failed", "file_format_check_failed"),
        ("FAILED", UPLOAD_URL, "unknown reason"),
    ],
)
def test_new_upload_processing_or_failed(status, reason, error_text):
    sleep = Mock()
    polls = Mock(return_value=response(status=status, fail_reason=reason))

    def route(request):
        if request.method == "PUT":
            return httpx.Response(201)
        if request.url.path.endswith("/init/"):
            return response(publish_id="new", upload_url=UPLOAD_URL)
        return polls()

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        channel = TikTokChannel(client, sleep=sleep)
        args = (ReelVideo(b"video", "", 10, False), "access", None, Mock())
        if error_text is None:
            assert channel.publish_with_ref(*args, claimed_at=NOW) is None
            assert polls.call_count == 6
            assert sleep.call_args_list == [call(10)] * 5
        else:
            with pytest.raises(ChannelError, match=error_text) as caught:
                channel.publish_with_ref(*args, claimed_at=NOW)
            assert UPLOAD_URL not in str(caught.value)
            polls.assert_called_once_with()
            sleep.assert_not_called()


@pytest.mark.parametrize(
    ("stage", "status", "code", "expected"),
    [
        ("init", 429, "rate_limit_exceeded", UncertainPublication),
        ("init", 400, "spam_risk_too_many_pending_share", UncertainPublication),
        ("fetch", 400, "spam_risk_too_many_posts", UncertainPublication),
        ("fetch", 403, "scope_not_authorized", ChannelError),
        ("put", 403, None, ChannelError),
        *[
            (stage, status, None, UncertainPublication)
            for stage in ("init", "put", "fetch")
            for status in (503, None)
        ],
    ],
)
def test_tiktok_error_classification(stage, status, code, expected, caplog):
    def route(request):
        if request.method == "PUT":
            endpoint = "put"
        else:
            endpoint = request.url.path.rsplit("/", 2)[-2]
        if endpoint != stage:
            if endpoint == "put":
                return httpx.Response(201)
            return response(publish_id="new", upload_url=UPLOAD_URL)
        if status is None:
            raise httpx.ReadError(UPLOAD_URL, request=request)
        if code is None:
            return httpx.Response(status, text="unavailable")
        return httpx.Response(
            status, json={"error": {"code": code, "message": UPLOAD_URL}}
        )

    with (
        httpx.Client(transport=httpx.MockTransport(route)) as client,
        caplog.at_level(logging.DEBUG),
        pytest.raises(expected) as caught,
    ):
        TikTokChannel(client).publish_with_ref(
            ReelVideo(b"video", "", 10, False), "access", None, Mock(), claimed_at=NOW
        )
    assert type(caught.value) is expected
    assert UPLOAD_URL not in str(caught.value)
    assert UPLOAD_URL not in caplog.text


def test_duration_validated_before_initializing_upload():
    route = Mock(side_effect=AssertionError("Invalid video uploaded"))
    with (
        httpx.Client(transport=httpx.MockTransport(route)) as client,
        pytest.raises(ChannelError, match="TikTok must be"),
    ):
        TikTokChannel(client).publish_with_ref(
            ReelVideo(b"video", "", 0, False), "access", None, Mock(), claimed_at=NOW
        )
