import subprocess
from collections.abc import Callable
from datetime import datetime
from functools import cache, partial
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from postpilot.channels.base import ChannelError, ChannelHeld
from postpilot.core.limits import shorten_text
from postpilot.core.models import Item, Variant
from postpilot.notify.telegram import TelegramApi, TelegramError
from postpilot.store.own_videos import OwnVideo
from postpilot.video.label import AI_VIDEO_CAPTION_LABEL
from postpilot.video.publisher import VideoChannel
from postpilot.video.reel import ReelVideo

_FIT = (
    "scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,"
    "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1"
)


def _fit_video(video: bytes, *, scale: bool) -> bytes:
    # Instagram rejects audio above 48 kHz (ProcessingFailedError), so the
    # audio is always re-encoded; the picture is only re-encoded when it must
    # be scaled and padded to 1080x1920.
    picture = ["-vf", _FIT, "-c:v", "libx264"] if scale else ["-c:v", "copy"]
    with TemporaryDirectory() as directory:
        source = Path(directory) / "source.mp4"
        output = Path(directory) / "reel.mp4"
        source.write_bytes(video)
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-i",
                    str(source),
                    *picture,
                    "-c:a",
                    "aac",
                    "-ar",
                    "48000",
                    "-movflags",
                    "+faststart",
                    str(output),
                ],
                check=True,
                capture_output=True,
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError):
            raise ChannelError("Reels video fitting failed") from None
        return output.read_bytes()


class OwnVideoReelsChannel:
    def __init__(
        self,
        channel: VideoChannel,
        get_video: Callable[[UUID], OwnVideo | None],
        telegram: TelegramApi,
        read_token: Callable[[str], str | None],
        *,
        caption_limit: int | None = None,
    ) -> None:
        self.name = channel.name
        self._channel = channel
        self._get_video = get_video
        self._telegram = telegram
        self._read_token = read_token
        self._caption_limit = caption_limit

    @staticmethod
    def _text(item: Item, variants: list[Variant], video: OwnVideo | None) -> str:
        if video is not None and not video.has_description:
            return ""
        for variant in variants:
            if (
                variant.item_id == item.id
                and variant.lang == "de"
                and variant.text.strip()
            ):
                return variant.text
        raise ChannelError("A German caption is required")

    def validate(self, item: Item, variants: list[Variant]) -> None:
        self._text(item, variants, self._get_video(item.id))

    def publish_with_ref(
        self,
        item: Item,
        variants: list[Variant],
        *,
        ref: str | None,
        claimed_at: datetime,
        save_ref: Callable[[str], None],
    ) -> str | None:
        token = self._read_token(self._channel.token_provider)
        if not token:
            raise ChannelHeld
        video = self._get_video(item.id)
        if video is None:
            raise ChannelError("Own video is unavailable")
        text = self._text(item, variants, video)
        label = AI_VIDEO_CAPTION_LABEL if video.ai_generated else ""
        if text and label:
            label = f"\n\n{label}"
        if self._caption_limit is not None:
            text = shorten_text(text, self._caption_limit - len(label))
        return self._channel.publish_with_ref(
            ReelVideo(
                cache(partial(self._video_bytes, video)),
                text + label,
                video.duration,
                video.ai_generated,
            ),
            token,
            ref,
            save_ref,
            claimed_at=claimed_at,
        )

    def _video_bytes(self, video: OwnVideo) -> bytes:
        try:
            content = self._telegram.download_file(
                self._telegram.get_file(video.file_id)
            )
        except TelegramError:
            raise ChannelError("Telegram video retrieval failed") from None
        if not content:
            raise ChannelError("Telegram video retrieval failed")
        width, height = video.width, video.height
        return _fit_video(
            content,
            scale=(
                width is None
                or height is None
                or width < 540
                or height < 960
                or width * 16 != height * 9
            ),
        )
