import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import BinaryIO

import httpx

_API = "https://open.tiktokapis.com/v2/post/publish"
_CHUNK_SIZE = 10 * 1024**2
_SINGLE_CHUNK_LIMIT = 64 * 1024**2
_uploading = ContextVar("tiktok_uploading", default=False)


def _exclude_upload_requests(record: logging.LogRecord) -> bool:
    # HTTPX logs the full URL, including the upload credential.
    return not _uploading.get()


logging.getLogger("httpx").addFilter(_exclude_upload_requests)


class TikTokAPIError(RuntimeError):
    def __init__(
        self, message: str, *, code: str = "-", uncertain: bool = False
    ) -> None:
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain


@dataclass(frozen=True)
class UploadPlan:
    video_size: int

    def __post_init__(self) -> None:
        if self.video_size <= 0:
            raise ValueError("TikTok video must be non-empty")

    @property
    def chunk_size(self) -> int:
        return (
            self.video_size if self.video_size <= _SINGLE_CHUNK_LIMIT else _CHUNK_SIZE
        )

    @property
    def total_chunk_count(self) -> int:
        return self.video_size // self.chunk_size


@dataclass(frozen=True)
class InboxUpload:
    publish_id: str
    upload_url: str = field(repr=False)


@dataclass(frozen=True)
class PublishStatus:
    status: str
    fail_reason: str | None

    @property
    def finished(self) -> bool:
        return self.status in {"SEND_TO_USER_INBOX", "PUBLISH_COMPLETE", "FAILED"}


@dataclass(frozen=True)
class CreatorInfo:
    privacy_level_options: list[str]
    comment_disabled: bool
    duet_disabled: bool
    stitch_disabled: bool
    max_video_post_duration_sec: int
    creator_nickname: str


class TikTokClient:
    def __init__(self, client: httpx.Client, access_token: str) -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {access_token}"}

    def init_inbox(self, plan: UploadPlan) -> InboxUpload:
        return self._init_upload("inbox/video/init", plan)

    def creator_info(self) -> CreatorInfo:
        data = self._post("creator_info/query", {})
        options = data.get("privacy_level_options")
        comment = data.get("comment_disabled")
        duet = data.get("duet_disabled")
        stitch = data.get("stitch_disabled")
        duration = data.get("max_video_post_duration_sec")
        nickname = data.get("creator_nickname")
        if (
            not isinstance(options, list)
            or not all(isinstance(option, str) and option for option in options)
            or not isinstance(comment, bool)
            or not isinstance(duet, bool)
            or not isinstance(stitch, bool)
            or not isinstance(duration, int)
            or isinstance(duration, bool)
            or duration <= 0
            or not isinstance(nickname, str)
            or not nickname.strip()
        ):
            raise TikTokAPIError("TikTok returned malformed creator info")
        return CreatorInfo(options, comment, duet, stitch, duration, nickname)

    def init_direct(
        self, plan: UploadPlan, post_info: dict[str, object]
    ) -> InboxUpload:
        return self._init_upload("video/init", plan, post_info=post_info)

    def _init_upload(
        self,
        endpoint: str,
        plan: UploadPlan,
        *,
        post_info: dict[str, object] | None = None,
    ) -> InboxUpload:
        body: dict[str, object] = {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": plan.video_size,
                "chunk_size": plan.chunk_size,
                "total_chunk_count": plan.total_chunk_count,
            }
        }
        if post_info is not None:
            body["post_info"] = post_info
        data = self._post(endpoint, body)
        publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
        if not isinstance(publish_id, str) or not publish_id:
            raise TikTokAPIError("TikTok did not return a publish ID")
        if not isinstance(upload_url, str) or not upload_url.startswith("https://"):
            raise TikTokAPIError("TikTok did not return an HTTPS upload URL")
        return InboxUpload(publish_id, upload_url)

    def upload(self, upload: InboxUpload, plan: UploadPlan, video: BinaryIO) -> None:
        for index in range(plan.total_chunk_count):
            start = index * plan.chunk_size
            stop = (
                plan.video_size
                if index == plan.total_chunk_count - 1
                else start + plan.chunk_size
            )
            content = video.read(stop - start)
            if len(content) != stop - start:
                raise TikTokAPIError("TikTok video ended before the expected size")
            upload_logging = _uploading.set(True)
            try:
                response = self._client.put(
                    upload.upload_url,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Range": f"bytes {start}-{stop - 1}/{plan.video_size}",
                    },
                    content=content,
                    timeout=120,
                    follow_redirects=False,
                )
            except (httpx.RequestError, httpx.StreamError):
                uncertain = True
            except httpx.InvalidURL:
                uncertain = False
            else:
                if response.is_success:
                    continue
                uncertain = response.status_code >= 500
            finally:
                _uploading.reset(upload_logging)
            raise TikTokAPIError("TikTok video upload failed", uncertain=uncertain)

    def status(self, publish_id: str) -> PublishStatus:
        data = self._post("status/fetch", {"publish_id": publish_id})
        status, reason = data.get("status"), data.get("fail_reason")
        if not isinstance(status, str) or not status:
            raise TikTokAPIError("TikTok did not return a publish status")
        return PublishStatus(status, reason if isinstance(reason, str) else None)

    def _post(self, endpoint: str, body: dict[str, object]) -> dict[str, object]:
        code = "-"
        uncertain = False
        try:
            response = self._client.post(
                f"{_API}/{endpoint}/",
                json=body,
                headers=self._headers,
                timeout=30,
                follow_redirects=False,
            )
            uncertain = response.status_code >= 500
            payload = response.json()
            if isinstance(payload, dict):
                error, data = payload.get("error"), payload.get("data")
                if isinstance(error, dict) and isinstance(error.get("code"), str):
                    code = re.sub(r"[^a-z0-9_]", "", error["code"])[:80] or "-"
                response.raise_for_status()
                if (
                    isinstance(error, dict)
                    and error.get("code") == "ok"
                    and isinstance(data, dict)
                ):
                    return data
        except (httpx.RequestError, httpx.StreamError):
            uncertain = True
        except (httpx.HTTPStatusError, httpx.InvalidURL, ValueError):
            pass
        raise TikTokAPIError(
            f"TikTok {endpoint} failed ({code})", code=code, uncertain=uncertain
        )
