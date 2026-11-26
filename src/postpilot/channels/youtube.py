import re
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from postpilot.channels.base import ChannelError, ChannelHeld, UncertainPublication
from postpilot.core.limits import REELS_MAX_DURATION, first_sentence
from postpilot.video.label import AI_VIDEO_CAPTION_LABEL
from postpilot.video.reel import ReelVideo

_UPLOAD = httpx.URL("https://www.googleapis.com/upload/youtube/v3/videos")
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]+")
_QUOTA_REASONS = ("quotaExceeded", "rateLimitExceeded", "userRateLimitExceeded")
_ANGLES = str.maketrans("<>", "‹›")


def fitting_title(text: str, override: str | None = None) -> str:
    if override is not None:
        title = override.strip()
        return title if len(title) <= 100 else ""
    text = text.strip()
    line = text.splitlines()[0].strip() if text else ""
    title = line if len(line) <= 100 else first_sentence(text)
    return title if len(title) <= 100 else ""


class YouTubeChannel:
    name = "youtube"
    token_provider = "youtube"

    def __init__(
        self,
        client: httpx.Client,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._clock = clock
        self._attempted = False

    def permalink(self, remote_id: str) -> str:
        return f"https://www.youtube.com/shorts/{quote(remote_id, safe='')}"

    def publish_with_ref(
        self,
        reel: ReelVideo,
        token: str,
        ref: str | None,
        save_ref: Callable[[str], None],
        *,
        claimed_at: datetime,
    ) -> str | None:
        if ref is not None and _VIDEO_ID.fullmatch(ref):
            return ref
        if self._attempted:
            raise ChannelHeld
        self._attempted = True
        reel.validate("YouTube", REELS_MAX_DURATION)
        description = reel.caption.translate(_ANGLES)
        if len(description.encode("utf-8")) > 5000:
            raise ChannelError("YouTube description exceeds 5000 UTF-8 bytes")

        video = reel.video_bytes()
        size = len(video)
        offset = 0
        if ref is not None:
            response = self._request(
                "PUT",
                self._session(ref),
                token,
                headers={"Content-Range": f"bytes */{size}", "Content-Length": "0"},
            )
            if response.status_code in (200, 201):
                return self._complete(response, save_ref)
            if response.status_code == 308:
                received = response.headers.get("Range")
                if received is not None:
                    match = re.fullmatch(r"bytes=0-(\d{1,20})", received)
                    if match is None or not 0 < (offset := int(match[1]) + 1) <= size:
                        raise ChannelError("Invalid YouTube upload range")
            elif response.status_code == 404:
                ref = None
            else:
                self._check_error(response)
        if ref is None:
            text = reel.caption
            if reel.ai_generated:
                text = text.removesuffix(AI_VIDEO_CAPTION_LABEL).removesuffix("\n\n")
            date = self._clock().astimezone(ZoneInfo("Europe/Berlin"))
            title = (
                fitting_title(text, reel.title)
                or f"Volodymyr Shemchuk · {date:%d.%m.%Y}"
            )
            response = self._request(
                "POST",
                _UPLOAD,
                token,
                params={"uploadType": "resumable", "part": "snippet,status"},
                headers={
                    "X-Upload-Content-Length": str(size),
                    "X-Upload-Content-Type": "video/mp4",
                },
                json={
                    "snippet": {
                        "title": title.translate(_ANGLES),
                        "description": description,
                        "categoryId": "28",
                    },
                    "status": {
                        "privacyStatus": "public",
                        "selfDeclaredMadeForKids": False,
                        "containsSyntheticMedia": reel.ai_generated,
                    },
                },
            )
            if not response.is_success:
                self._check_error(response)
            ref = str(self._session(response.headers.get("Location", "")))
        if offset == size:
            return None
        session = self._session(ref)
        save_ref(ref)
        response = self._request(
            "PUT",
            session,
            token,
            headers={
                "Content-Range": f"bytes {offset}-{size - 1}/{size}",
                "Content-Type": "video/mp4",
            },
            content=video[offset:],
        )
        if response.status_code == 308:
            return None
        if response.status_code in (200, 201):
            return self._complete(response, save_ref)
        self._check_error(response)
        return None

    @staticmethod
    def _session(ref: str) -> httpx.URL:
        try:
            url = httpx.URL(ref)
        except httpx.InvalidURL:
            raise ChannelError("Invalid YouTube upload session") from None
        if (
            url.scheme != "https"
            or url.host not in {"www.googleapis.com", "youtube.googleapis.com"}
            or url.port not in {None, 443}
            or url.userinfo
            or url.path != _UPLOAD.path
            or not url.params.get("upload_id")
        ):
            raise ChannelError("Invalid YouTube upload session")
        return url

    def _request(
        self,
        method: str,
        url: httpx.URL,
        token: str,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
        json: dict[str, object] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        try:
            return self._client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}", **headers},
                params=params,
                json=json,
                content=content,
                timeout=120,
                follow_redirects=False,
            )
        except (httpx.RequestError, httpx.InvalidURL):
            raise UncertainPublication("YouTube request failed") from None

    @staticmethod
    def _complete(response: httpx.Response, save_ref: Callable[[str], None]) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        video_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id):
            raise UncertainPublication("YouTube did not return a video ID")
        save_ref(video_id)
        return video_id

    @staticmethod
    def _check_error(response: httpx.Response) -> None:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        error = payload.get("error") if isinstance(payload, dict) else None
        details = error.get("errors") if isinstance(error, dict) else None
        if response.status_code == 429 or (
            isinstance(details, list)
            and any(
                isinstance(entry, dict) and entry.get("reason") in _QUOTA_REASONS
                for entry in details
            )
        ):
            raise UncertainPublication("YouTube quota or rate limit reached")
        if response.status_code >= 500:
            raise UncertainPublication("YouTube service unavailable")
        raise ChannelError(f"YouTube request rejected (HTTP {response.status_code})")
