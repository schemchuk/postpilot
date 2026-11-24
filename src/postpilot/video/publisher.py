import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from functools import partial
from typing import Protocol

from postpilot.channels.base import ChannelHeld, TransientChannelError
from postpilot.store.repo import LostPublicationClaim
from postpilot.video.label import AI_AVATAR_CAPTION_LABEL
from postpilot.video.publications import VideoPublicationStore
from postpilot.video.reel import ReelVideo
from postpilot.video.store import VideoStatus, VideoStore

logger = logging.getLogger(__name__)
_STALE_AFTER = timedelta(minutes=15)


class VideoChannel(Protocol):
    name: str
    token_provider: str

    def publish_with_ref(
        self,
        reel: ReelVideo,
        token: str,
        ref: str | None,
        save_ref: Callable[[str], None],
        *,
        claimed_at: datetime,
    ) -> str | None: ...

    def permalink(self, remote_id: str) -> str: ...


def alert(notify: Callable[[str], None], message: str) -> None:
    try:
        notify(message)
    except Exception as error:  # noqa: BLE001 - isolate Telegram failures
        logger.warning("Reels alert failed (%s)", type(error).__name__)


def publish_one(
    store: VideoPublicationStore,
    renders: VideoStore,
    channels: Sequence[VideoChannel],
    read_token: Callable[[str], str | None],
    notify: Callable[[str], None],
    *,
    retry: bool = False,
) -> None:
    render_id = store.oldest_eligible(
        {channel.name for channel in channels}, _STALE_AFTER, retry=retry
    )
    render = renders.get(render_id) if render_id is not None else None
    if render is None or render.status != VideoStatus.APPROVED:
        return

    reel = ReelVideo(
        render.video or b"",
        f"{render.script}\n\n{AI_AVATAR_CAPTION_LABEL}"
        if render.script and render.script.strip()
        else "",
        render.duration,
        ai_generated=True,
    )

    held = set()
    for channel in channels:
        claim = store.claim(render.id, channel.name, _STALE_AFTER, retry=retry)
        if claim is None:
            continue
        try:
            token = read_token(channel.token_provider)
            if not token:
                raise ChannelHeld
            remote_id = channel.publish_with_ref(
                reel,
                token,
                claim.remote_ref,
                partial(store.save_ref, claim),
                claimed_at=claim.remote_ref_claimed_at or claim.updated_at,
            )
        except ChannelHeld:
            store.release(claim)
            held.add(channel.token_provider)
        except (TransientChannelError, LostPublicationClaim):
            continue
        except Exception as error:  # noqa: BLE001 - isolate adapter failures
            message = f"Channel operation failed ({type(error).__name__})"
            store.mark_failed(claim, message)
            alert(notify, f"{channel.name}: {message}")
        else:
            if remote_id is not None:
                store.mark_published(claim, remote_id)
                alert(
                    notify, f"{channel.name}: published {channel.permalink(remote_id)}"
                )
    if held:
        alert(
            notify,
            f"Reels held: missing or invalid token for {', '.join(sorted(held))}",
        )
