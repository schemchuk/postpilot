"""LinkedIn video publication with per-channel references.

video:<urn> records an upload with unknown progress and video-final:<urn> records
a finalized upload. video-posting:<urn> records a post attempt that must be
reconciled by marker before the publication can be marked published.
"""

import logging
import time
from collections.abc import Callable
from contextvars import ContextVar
from datetime import datetime
from urllib.parse import quote
from uuid import UUID

import httpx

from postpilot.channels.base import (
    ChannelError,
    ChannelHeld,
    TransientChannelError,
    UncertainPublication,
)
from postpilot.channels.linkedin import _VIDEOS, LinkedInChannel
from postpilot.core.models import Item, Variant
from postpilot.notify.telegram import TelegramError
from postpilot.store.repo import ItemRepo
from postpilot.video.label import AI_AVATAR_CAPTION_LABEL, AI_VIDEO_CAPTION_LABEL
from postpilot.video.store import VideoRender

_UPLOAD = "video:"
_FINAL = "video-final:"
_POSTING = "video-posting:"
_POLL_ATTEMPTS = 6
_POLL_INTERVAL = 5
_PART_ATTEMPTS = 3
_FILE_LIMIT = 20_000_000
_uploading_part = ContextVar("linkedin_uploading_part", default=False)


def _exclude_upload_urls(record: logging.LogRecord) -> bool:
    # HTTPX logs complete signed upload URLs.
    return not _uploading_part.get()


logging.getLogger("httpx").addFilter(_exclude_upload_urls)


class _LinkedInVideoPublisher:
    token_provider = LinkedInChannel.token_provider

    def __init__(
        self, linkedin: LinkedInChannel, sleep: Callable[[float], None]
    ) -> None:
        self.name = linkedin.name
        self._linkedin = linkedin
        self._sleep = sleep

    def _upload_part(self, url: str, data: bytes) -> str:
        for attempt in range(_PART_ATTEMPTS):
            logging_context = _uploading_part.set(True)
            try:
                client = self._linkedin._client
                request = client.build_request(
                    "PUT",
                    url,
                    content=data,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=30,
                )
                request.headers.pop("Authorization", None)
                response = client.send(request, auth=None, follow_redirects=False)
            except (httpx.RequestError, httpx.StreamError):
                pass
            except (ValueError, httpx.InvalidURL):
                raise ChannelError("Invalid LinkedIn upload URL") from None
            else:
                if response.is_success:
                    etag = response.headers.get("ETag")
                    if isinstance(etag, str) and etag:
                        return etag
                    raise ChannelError("LinkedIn upload part did not return an ETag")
                if response.status_code < 500:
                    raise ChannelError("LinkedIn upload part rejected")
            finally:
                _uploading_part.reset(logging_context)
            if attempt + 1 < _PART_ATTEMPTS:
                self._sleep(float(2**attempt))
        raise TransientChannelError("LinkedIn upload part failed")

    def _upload(
        self, data: bytes, author: str, token: str, save_ref: Callable[[str], None]
    ) -> str:
        if not data:
            raise ChannelError("An approved video is required")
        linkedin = self._linkedin
        response = linkedin._request(
            "POST",
            _VIDEOS,
            linkedin._same_token(token),
            params={"action": "initializeUpload"},
            payload={
                "initializeUploadRequest": {
                    "owner": author,
                    "fileSizeBytes": len(data),
                    "uploadCaptions": False,
                    "uploadThumbnail": False,
                }
            },
        )
        value = linkedin._object(response).get("value")
        urn = value.get("video") if isinstance(value, dict) else None
        if not isinstance(urn, str) or not urn.startswith("urn:li:video:"):
            raise ChannelError("LinkedIn initializeUpload did not return a video URN")
        save_ref(_UPLOAD + urn)
        assert isinstance(value, dict)
        upload_token = value.get("uploadToken")
        parts = value.get("uploadInstructions")
        if (
            not isinstance(upload_token, str)
            or not isinstance(parts, list)
            or not parts
        ):
            raise ChannelError("Invalid LinkedIn upload instructions")

        etags = []
        for part in parts:
            if not isinstance(part, dict):
                raise ChannelError("Invalid LinkedIn upload part")
            url, first, last = (
                part.get("uploadUrl"),
                part.get("firstByte"),
                part.get("lastByte"),
            )
            if (
                not isinstance(url, str)
                or not isinstance(first, int)
                or not isinstance(last, int)
                or not 0 <= first <= last < len(data)
            ):
                raise ChannelError("Invalid LinkedIn upload part")
            etags.append(self._upload_part(url, data[first : last + 1]))

        linkedin._request(
            "POST",
            _VIDEOS,
            linkedin._same_token(token),
            params={"action": "finalizeUpload"},
            payload={
                "finalizeUploadRequest": {
                    "video": urn,
                    "uploadToken": upload_token,
                    "uploadedPartIds": etags,
                }
            },
        )
        save_ref(_FINAL + urn)
        return urn

    def _available(self, urn: str, token: str) -> bool:
        linkedin = self._linkedin
        url = f"{_VIDEOS}/{quote(urn, safe='')}"
        for attempt in range(_POLL_ATTEMPTS):
            response = linkedin._request("GET", url, linkedin._same_token(token))
            status = linkedin._object(response).get("status")
            if status == "AVAILABLE":
                return True
            if status == "PROCESSING_FAILED":
                raise ChannelError("LinkedIn video processing failed")
            if status not in ("PROCESSING", "WAITING_UPLOAD"):
                raise ChannelError("Unexpected LinkedIn video status")
            if attempt + 1 < _POLL_ATTEMPTS:
                self._sleep(_POLL_INTERVAL)
        return False

    def _publish(
        self,
        item_id: UUID,
        token: str,
        commentary: Callable[[], str],
        video_bytes: Callable[[], bytes],
        ref: str | None,
        save_ref: Callable[[str], None],
    ) -> str | None:
        linkedin = self._linkedin
        if ref and ref.startswith(_POSTING):
            try:
                return linkedin._find_existing(item_id, token)
            except ChannelError:
                raise UncertainPublication("LinkedIn post lookup failed") from None

        text = commentary()
        author = linkedin._author(token)
        resumed = ref is not None and ref.startswith(_FINAL)
        if resumed and ref is not None:
            urn = ref.removeprefix(_FINAL)
        else:
            urn = self._upload(video_bytes(), author, token, save_ref)
        try:
            if not self._available(urn, token):
                return None
            if resumed and (existing := linkedin._find_existing(item_id, token)):
                return existing
        except TransientChannelError:
            # Keep the finalized upload for the next run.
            return None

        save_ref(_POSTING + urn)
        try:
            return linkedin._create_post(text, author, token, video=urn)
        except UncertainPublication:
            raise
        except TransientChannelError:
            save_ref(_FINAL + urn)
            return None
        except (ChannelError, ChannelHeld):
            save_ref(_FINAL + urn)
            raise


class LinkedInVideoChannel(_LinkedInVideoPublisher):
    def __init__(
        self,
        linkedin: LinkedInChannel,
        *,
        fetch_video: Callable[[UUID], bytes],
        ai_generated: Callable[[UUID], bool],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(linkedin, sleep)
        self._fetch_video = fetch_video
        self._ai_generated = ai_generated

    def _commentary(self, item: Item, variants: list[Variant]) -> str:
        label = AI_VIDEO_CAPTION_LABEL if self._ai_generated(item.id) else ""
        return self._linkedin._commentary(item, variants, label=label)

    def validate(self, item: Item, variants: list[Variant]) -> None:
        self._commentary(item, variants)

    def _video_bytes(self, item_id: UUID) -> bytes:
        try:
            data = self._fetch_video(item_id)
        except TelegramError:
            raise ChannelError("Telegram video retrieval failed") from None
        if not 0 < len(data) <= _FILE_LIMIT:
            raise ChannelError("Invalid LinkedIn video size")
        return data

    def publish_with_ref(
        self,
        item: Item,
        variants: list[Variant],
        *,
        ref: str | None,
        claimed_at: datetime,
        save_ref: Callable[[str], None],
    ) -> str | None:
        return self._publish(
            item.id,
            self._linkedin._token(),
            lambda: self._commentary(item, variants),
            lambda: self._video_bytes(item.id),
            ref,
            save_ref,
        )


class LinkedInRenderChannel(_LinkedInVideoPublisher):
    def __init__(
        self,
        linkedin: LinkedInChannel,
        repo: ItemRepo,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(linkedin, sleep)
        self._repo = repo

    def publish_render(
        self,
        render: VideoRender,
        token: str,
        ref: str | None,
        save_ref: Callable[[str], None],
    ) -> str | None:
        def commentary() -> str:
            item = self._repo.get_item(render.item_id)
            if item is None:
                raise ChannelError("Render item is unavailable")
            return self._linkedin._commentary(
                item,
                self._repo.get_variants(item.id),
                leading_label=AI_AVATAR_CAPTION_LABEL,
            )

        return self._publish(
            render.item_id,
            token,
            commentary,
            lambda: render.video or b"",
            ref,
            save_ref,
        )

    def permalink(self, remote_id: str) -> str:
        return f"https://www.linkedin.com/feed/update/{quote(remote_id, safe=':')}/"
