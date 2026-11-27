import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from io import BytesIO

import httpx

from postpilot.channels.base import (
    ChannelError,
    ChannelHeld,
    ChannelSkipped,
    UncertainPublication,
)
from postpilot.core.limits import REELS_MAX_DURATION
from postpilot.core.tiktok import TikTokMode
from postpilot.video.reel import ReelVideo
from postpilot.video.tiktok import TikTokAPIError, TikTokClient, UploadPlan

_INBOX = "SEND_TO_USER_INBOX"
_COMPLETE = "PUBLISH_COMPLETE"
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
        mode: TikTokMode = "draft",
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self.mode = mode
        self._sleep = sleep
        self._clock = clock
        self._attempted = False
        self._captions: dict[str, str] = {}

    def permalink(self, remote_id: str) -> str:
        return ""

    def caption(self, publish_id: str) -> str | None:
        return self._captions.get(publish_id)

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
            if ref is not None:
                return None
            raise ChannelHeld
        api = TikTokClient(self._client, token)
        choice = reel.tiktok
        inbox = self.mode == "draft" or (choice is None and reel.inbox_fallback)
        try:
            if ref is not None:
                self._attempted = True
                status = api.status(ref)
                if status.status == _COMPLETE:
                    if inbox:
                        self._captions[ref] = reel.caption
                    return ref
                if status.status == _INBOX and inbox:
                    self._captions[ref] = reel.caption
                    return ref
                expired = (
                    status.status in _PROCESSING
                    and self._clock() - claimed_at > _UPLOAD_LIFETIME
                )
                if status.status != "FAILED" and not expired:
                    return None

            post_info = None
            if self.mode == "direct":
                if choice is None and not inbox:
                    raise ChannelHeld
                if len(reel.caption) > 2200:
                    raise ChannelSkipped(
                        "caption exceeds TikTok's 2200-character limit"
                    )
                if choice is not None:
                    self._attempted = True
                    creator = api.creator_info()
                    if choice.privacy_level not in creator.privacy_level_options:
                        raise ChannelError(
                            "Chosen TikTok privacy level is no longer offered"
                        )
                    reel.validate("TikTok", creator.max_video_post_duration_sec)
                    post_info = {
                        "title": reel.caption,
                        "is_aigc": reel.ai_generated,
                        "video_cover_timestamp_ms": 0,
                        "brand_content_toggle": False,
                        "brand_organic_toggle": False,
                        "privacy_level": choice.privacy_level,
                        "disable_comment": creator.comment_disabled
                        or not choice.allow_comment,
                        "disable_duet": creator.duet_disabled or not choice.allow_duet,
                        "disable_stitch": creator.stitch_disabled
                        or not choice.allow_stitch,
                    }
            if inbox:
                reel.validate("TikTok", REELS_MAX_DURATION)
            video = reel.video_bytes()
            plan = UploadPlan(len(video))
            self._attempted = True
            upload = (
                api.init_inbox(plan)
                if post_info is None
                else api.init_direct(plan, post_info)
            )
            save_ref(upload.publish_id)
            api.upload(upload, plan, BytesIO(video))
            for attempt in range(_PROCESSING_POLLS):
                if attempt:
                    self._sleep(_PROCESSING_INTERVAL)
                status = api.status(upload.publish_id)
                if status.status == _COMPLETE or (inbox and status.status == _INBOX):
                    if inbox:
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
