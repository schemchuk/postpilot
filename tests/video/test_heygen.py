import json
import logging
from pathlib import Path

import httpx
import pytest

from postpilot.config import HeyGenSettings
from postpilot.video.heygen import HeyGenClient, HeyGenError

_SETTINGS = HeyGenSettings("mock-private-value", "photo-look", "voice-de")


def fixture(name):
    return json.loads((Path(__file__).parent / "fixtures" / f"{name}.json").read_text())


@pytest.fixture
def make_client():
    clients = []

    def make(handler, **kwargs):
        client = HeyGenClient(
            _SETTINGS, transport=httpx.MockTransport(handler), **kwargs
        )
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


def test_create_poll_download_and_balance(make_client, caplog):
    caplog.set_level(logging.DEBUG)
    responses = iter(["created", "processing", "completed", "wallet"])
    calls = []
    sleeps = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.host == "cdn.example.test":
            assert request.url.path == "/captioned.mp4"
            assert "x-api-key" not in request.headers
            return httpx.Response(200, content=b"mock mp4")
        assert request.url.host == "api.heygen.com"
        assert request.headers["x-api-key"] == _SETTINGS.api_key
        if request.method == "POST":
            assert json.loads(request.content) == {
                "type": "avatar",
                "avatar_id": "photo-look",
                "voice_id": "voice-de",
                "script": "Guten Morgen!",
                "title": "Wochenrückblick",
                "aspect_ratio": "9:16",
                "resolution": "1080p",
                "engine": {"type": "avatar_iv"},
                "voice_settings": {
                    "speed": 1.0,
                    "engine_settings": {
                        "engine_type": "heygen_voice",
                        "model": "heygen-voice-1",
                    },
                },
                "caption": {"file_format": "srt", "style": "default"},
            }
        return httpx.Response(200, json=fixture(next(responses)))

    client = make_client(handler, sleep=sleeps.append)
    video_id = client.create_video("Guten Morgen!", "Wochenrückblick")
    assert video_id == "render-123"
    result = client.wait_for_video(video_id, interval=2)
    assert result.duration == 24.5
    assert result.subtitle_url.endswith("/subtitles.srt?signature=mock")
    assert result.thumbnail_url.endswith("/thumbnail.jpg?signature=mock")
    assert result.video_url.endswith("/clean.mp4?signature=mock")
    assert client.download(result.captioned_video_url) == b"mock mp4"
    assert client.wallet_balance() == 18.25
    assert sleeps == [2]
    assert calls == [
        ("POST", "/v3/videos"),
        ("GET", "/v3/videos/render-123"),
        ("GET", "/v3/videos/render-123"),
        ("GET", "/captioned.mp4"),
        ("GET", "/v3/users/me"),
    ]
    assert _SETTINGS.api_key not in caplog.text


def test_create_accepts_id_fallback(make_client):
    client = make_client(
        lambda _: httpx.Response(200, json={"data": {"id": "fallback"}})
    )
    assert client.create_video("script", "title") == "fallback"


@pytest.mark.parametrize("disconnect", [False, True])
def test_post_failure_is_sanitized_and_never_retried(make_client, disconnect):
    calls = []

    def handler(request):
        calls.append(request)
        if disconnect:
            raise httpx.ReadTimeout(_SETTINGS.api_key, request=request)
        return httpx.Response(503, text=_SETTINGS.api_key)

    with pytest.raises(HeyGenError, match="^HeyGen API request failed$") as error:
        make_client(handler).create_video("script", "title")
    assert error.value.__context__ is None
    assert len(calls) == 1


@pytest.mark.parametrize("name", ["failed", "completed"])
def test_failed_or_uncaptioned_render_is_rejected(make_client, name):
    payload = fixture(name)
    payload["data"].pop("captioned_video_url", None)
    client = make_client(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(
        HeyGenError, match="rendering failed|without a captioned video"
    ) as error:
        client.wait_for_video("render-123")
    assert "https://" not in str(error.value)


def test_polling_deadline_caps_sleep(make_client):
    sleeps = []
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=fixture("processing"))

    client = make_client(handler, sleep=sleeps.append, now=lambda: sum(sleeps))
    with pytest.raises(HeyGenError, match="timed out"):
        client.wait_for_video("render-123", timeout=5, interval=2)
    assert sleeps == [2, 2, 1]
    assert len(requests) == 3


def test_download_error_hides_presigned_url(make_client):
    client = make_client(lambda _: httpx.Response(403))
    with pytest.raises(HeyGenError, match="^HeyGen video download failed$") as error:
        client.download(fixture("completed")["data"]["captioned_video_url"])
    assert error.value.__context__ is None
