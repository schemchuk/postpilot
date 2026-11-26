from collections.abc import Callable
from dataclasses import dataclass

from postpilot.channels.base import ChannelError
from postpilot.core.limits import REELS_MIN_DURATION
from postpilot.video.label import AI_AVATAR_CAPTION_LABEL, AI_VIDEO_CAPTION_LABEL


@dataclass(frozen=True)
class ReelVideo:
    # A factory loader lets adapters recover published refs without the release bytes.
    video: bytes | Callable[[], bytes]
    caption: str
    duration: float | None
    ai_generated: bool

    def video_bytes(self) -> bytes:
        video = self.video() if callable(self.video) else self.video
        if not video:
            raise ChannelError("An approved video is required")
        return video

    def validate(self, channel: str, max_duration: int) -> None:
        if not self.video:
            raise ChannelError("An approved video is required")
        if self.caption and not self.caption.strip():
            raise ChannelError("A video caption is required")
        if (
            self.ai_generated
            and self.caption != AI_VIDEO_CAPTION_LABEL
            and not self.caption.endswith(
                (f"\n\n{AI_AVATAR_CAPTION_LABEL}", f"\n\n{AI_VIDEO_CAPTION_LABEL}")
            )
        ):
            raise ChannelError("An AI-generated video label is required")
        if self.duration is not None and not (
            REELS_MIN_DURATION <= self.duration <= max_duration
        ):
            raise ChannelError(
                f"{channel} must be {REELS_MIN_DURATION} to {max_duration} seconds long"
            )
