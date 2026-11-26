import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import BytesIO

import httpx

from postpilot.channels.base import ChannelError, ChannelHeld, UncertainPublication
from postpilot.core.limits import REELS_MAX_DURATION
from postpilot.video.reel import ReelVideo
from postpilot.video.tiktok import TikTokAPIError, TikTokClient, UploadPlan

_DONE = {"SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"}
_PROCESSING = {"PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD"}
_HELD_CODES = {
    "rate_limit_exceeded",
    "spam_risk_too_many_pending_share",
    "spam_risk_too_many_posts",
}
_UPLOAD_LIFETIME = timedelta(hours=1)
_PROCESSING_POLLS = 6
_PROCESSING_INTERVAL = 10


class TikTokChannel:
    name = "tiktok"
    token_provider = "tiktok"

    def __init__(
        self,
        client: httpx.Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._sleep = sleep
        self._clock = clock
        self._attempted = False
        self._captions: dict[str, str] = {}

    def permalink(self, remote_id: str) -> str:
        return ""

    def caption(self, publish_id: str) -> str:
        return self._captions[publish_id]

    def publish_with_ref(
        self,
        reel: ReelVideo,
        token: str,
        ref: str | None,
        save_ref: Callable[[str], None],
        *,
        claimed_at: datetime,
    ) -> str | None:
        if self._attempted:
            raise ChannelHeld
        self._attempted = True
        api = TikTokClient(self._client, token)
        try:
            if ref is not None:
                status = api.status(ref)
                if status.status in _DONE:
                    self._captions[ref] = reel.caption
                    return ref
                expired = (
                    status.status in _PROCESSING
                    and self._clock() - claimed_at > _UPLOAD_LIFETIME
                )
                if status.status != "FAILED" and not expired:
                    return None

            reel.validate("TikTok", REELS_MAX_DURATION)
            video = reel.video_bytes()
            plan = UploadPlan(len(video))
            upload = api.init_inbox(plan)
            save_ref(upload.publish_id)
            api.upload(upload, plan, BytesIO(video))
            for attempt in range(_PROCESSING_POLLS):
                if attempt:
                    self._sleep(_PROCESSING_INTERVAL)
                status = api.status(upload.publish_id)
                if status.status in _DONE:
                    self._captions[upload.publish_id] = reel.caption
                    return upload.publish_id
                if status.status == "FAILED":
                    reason = status.fail_reason or ""
                    if not re.fullmatch(r"[a-z0-9_]{1,80}", reason):
                        reason = "unknown reason"
                    raise ChannelError(f"TikTok upload failed ({reason})")
        except TikTokAPIError as error:
            if error.uncertain or error.code in _HELD_CODES:
                raise UncertainPublication(str(error)) from None
            raise ChannelError(str(error)) from None
        return None
