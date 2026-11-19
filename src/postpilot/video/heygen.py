import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import quote

import httpx

from postpilot.config import HeyGenSettings

_BASE = "https://api.heygen.com"


class HeyGenError(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoResult:
    captioned_video_url: str = field(repr=False)
    video_url: str | None = field(repr=False)
    subtitle_url: str | None = field(repr=False)
    thumbnail_url: str | None = field(repr=False)
    duration: float | None


class HeyGenClient:
    def __init__(
        self,
        settings: HeyGenSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._client = httpx.Client(transport=transport, timeout=30.0)
        self._sleep = sleep
        self._now = now

    def close(self) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, object]:
        try:
            response = self._client.request(
                method,
                f"{_BASE}{path}",
                json=payload,
                timeout=timeout,
                headers={
                    "x-api-key": self._settings.api_key,
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
            data = response.json()["data"]
            if not isinstance(data, dict):
                raise TypeError
            return cast(dict[str, object], data)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, TypeError, KeyError):
            pass
        raise HeyGenError("HeyGen API request failed")

    def create_video(self, script: str, title: str) -> str:
        data = self._request(
            "POST",
            "/v3/videos",
            payload={
                "type": "avatar",
                "avatar_id": self._settings.avatar_id,
                "script": script,
                "voice_id": self._settings.voice_id,
                "title": title,
                "aspect_ratio": "9:16",
                "resolution": "1080p",
                "engine": {"type": self._settings.engine},
                "voice_settings": {
                    "speed": 1.0,
                    "engine_settings": {
                        "engine_type": "heygen_voice",
                        "model": "heygen-voice-1",
                    },
                },
                "caption": {"file_format": "srt", "style": "default"},
            },
        )
        video_id = _text(data.get("video_id")) or _text(data.get("id"))
        if video_id is None:
            raise HeyGenError("HeyGen response has no video ID")
        return video_id

    def wait_for_video(
        self, video_id: str, *, timeout: float = 600.0, interval: float = 10.0
    ) -> VideoResult:
        deadline = self._now() + timeout
        while (remaining := deadline - self._now()) > 0:
            data = self._request(
                "GET",
                f"/v3/videos/{quote(video_id, safe='')}",
                timeout=min(remaining, 30.0),
            )
            if self._now() >= deadline:
                break
            if data.get("status") == "failed":
                raise HeyGenError("HeyGen video rendering failed")
            if data.get("status") == "completed":
                captioned_url = _text(data.get("captioned_video_url"))
                if captioned_url is None:
                    raise HeyGenError("HeyGen completed without a captioned video")
                duration = data.get("duration")
                return VideoResult(
                    captioned_url,
                    _text(data.get("video_url")),
                    _text(data.get("subtitle_url")),
                    _text(data.get("thumbnail_url")),
                    float(duration)
                    if isinstance(duration, (int, float))
                    and not isinstance(duration, bool)
                    else None,
                )
            remaining = deadline - self._now()
            if remaining > 0:
                self._sleep(min(interval, remaining))
        raise HeyGenError("HeyGen video polling timed out")

    def download(self, url: str) -> bytes:
        # Presigned media URLs authenticate themselves.
        try:
            response = self._client.get(url)
            response.raise_for_status()
            return response.content
        except (httpx.HTTPError, httpx.InvalidURL):
            pass
        raise HeyGenError("HeyGen video download failed")

    def wallet_balance(self) -> float:
        wallet = self._request("GET", "/v3/users/me").get("wallet")
        balance = wallet.get("remaining_balance") if isinstance(wallet, dict) else None
        if (
            not isinstance(balance, (int, float))
            or isinstance(balance, bool)
            or not math.isfinite(balance)
        ):
            raise HeyGenError("HeyGen response has no valid wallet balance")
        return float(balance)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
