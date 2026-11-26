from collections.abc import Callable
from datetime import datetime
from functools import partial
from hashlib import sha256
from urllib.parse import quote
from uuid import UUID

import httpx

from postpilot.channels.base import ChannelError, ChannelHeld
from postpilot.channels.linkedin import LinkedInChannel
from postpilot.channels.linkedin_video import LinkedInVideoChannel
from postpilot.channels.own_video_reels import _fit_video
from postpilot.channels.threads import ThreadsChannel
from postpilot.core.models import Item, Variant
from postpilot.sources.factory import FactoryRelease, Rejected
from postpilot.store.factory_reels import Asset, FactoryReel, FactoryReelStore
from postpilot.video.label import AI_VIDEO_CAPTION_LABEL
from postpilot.video.publisher import VideoChannel
from postpilot.video.reel import ReelVideo

_VIDEO = "reel_de.mp4"


class FactoryAssets:
    def __init__(
        self, store: FactoryReelStore, client: httpx.Client, repo: str
    ) -> None:
        self.store = store
        self.client = client
        self.repo = repo
        # The job processes one item at a time. Keep both versions only for that item.
        self._key: tuple[UUID, Asset | None] | None = None
        self._raw: bytes | None = None
        self._fitted: bytes | None = None
        self._error: str | None = None

    def reel(self, item_id: UUID) -> FactoryReel:
        reel = self.store.get_by_item(item_id)
        if reel is None:
            raise ChannelError("Factory reel data is unavailable")
        return reel

    def _release_video(self, reel: FactoryReel) -> tuple[FactoryRelease, httpx.URL]:
        try:
            release = FactoryRelease(self.client, self.repo, reel.tag)
        except Rejected:
            raise ChannelError(f"Factory release {reel.tag} is unavailable") from None
        try:
            return release, release.asset_url(_VIDEO)
        except Rejected as error:
            raise ChannelError(f"Factory {error}") from None

    def check_release(self, item_id: UUID) -> None:
        reel = self.reel(item_id)
        if (item_id, reel.assets.get(_VIDEO)) != self._key:
            self._release_video(reel)
        elif self._error is not None:
            raise ChannelError(self._error)

    def video(self, item_id: UUID) -> bytes:
        reel = self.reel(item_id)
        expected = reel.assets.get(_VIDEO)
        key = (item_id, expected)
        if self._key != key:
            self._key = key
            self._fitted = self._raw = self._error = None
            try:
                release, url = self._release_video(reel)
                video = release.download(_VIDEO, url)
                if expected != {
                    "size": len(video),
                    "sha256": sha256(video).hexdigest(),
                }:
                    raise ChannelError(f"Factory asset {_VIDEO} changed since intake")
                self._raw = video
            except (ChannelError, Rejected) as error:
                self._error = str(error)
        if self._raw is None:
            raise ChannelError(self._error)
        return self._raw

    def fitted_video(self, item_id: UUID) -> bytes:
        raw = self.video(item_id)
        if self._fitted is None:
            # Intake stores duration only. Keep the release picture and normalize audio.
            self._fitted = _fit_video(raw, scale=False)
        return self._fitted


class FactoryLinkedInChannel(LinkedInVideoChannel):
    _verbatim = True
    _file_limit = None

    def __init__(self, linkedin: LinkedInChannel, assets: FactoryAssets) -> None:
        super().__init__(
            linkedin, fetch_video=assets.video, ai_generated=lambda _: False
        )
        self._assets = assets

    def _commentary(self, item: Item, variants: list[Variant]) -> str:
        reel = self._assets.reel(item.id)
        return self._linkedin._commentary(
            item,
            [Variant(item.id, "de", reel.post_de)],
            label=AI_VIDEO_CAPTION_LABEL if reel.ai else "",
            include_marker=False,
        )


class FactoryThreadsChannel(ThreadsChannel):
    def __init__(
        self,
        token_source: Callable[[], str | None],
        client: httpx.Client,
        assets: FactoryAssets,
    ) -> None:
        super().__init__(token_source, client)
        self._assets = assets

    def _text(self, item: Item, variants: list[Variant]) -> str:
        text = self._assets.reel(item.id).post_threads
        if text is None or not text.strip():
            raise ChannelError("Factory Threads text is empty")
        if len(text) > 500:
            raise ChannelError("Factory Threads text exceeds 500 characters")
        return text

    def _before_publish(self, item: Item) -> None:
        self._assets.check_release(item.id)

    def permalink(self, remote_id: str) -> str:
        try:
            link = self._request(
                "GET",
                quote(remote_id, safe=""),
                self._token(),
                params={"fields": "permalink"},
            ).get("permalink")
        except (ChannelError, ChannelHeld):
            link = None
        return link if isinstance(link, str) and link else f"Threads post {remote_id}"


class FactoryReelsChannel:
    def __init__(
        self,
        channel: VideoChannel,
        read_token: Callable[[str], str | None],
        assets: FactoryAssets,
        *,
        caption_limit: int | None = None,
    ) -> None:
        self.name = channel.name
        self.token_provider = channel.token_provider
        self._channel = channel
        self._read_token = read_token
        self._assets = assets
        self._caption_limit = caption_limit

    def _caption(self, reel: FactoryReel) -> str:
        text = reel.post_de + (f"\n\n{AI_VIDEO_CAPTION_LABEL}" if reel.ai else "")
        if self._caption_limit is not None and len(text) > self._caption_limit:
            raise ChannelError(
                f"Factory caption exceeds {self._caption_limit} characters"
            )
        return text

    def validate(self, item: Item, variants: list[Variant]) -> None:
        self._caption(self._assets.reel(item.id))

    def publish_with_ref(
        self,
        item: Item,
        variants: list[Variant],
        *,
        ref: str | None,
        claimed_at: datetime,
        save_ref: Callable[[str], None],
    ) -> str | None:
        token = self._read_token(self.token_provider)
        if not token:
            raise ChannelHeld
        reel = self._assets.reel(item.id)
        caption = self._caption(reel)
        return self._channel.publish_with_ref(
            ReelVideo(
                partial(self._assets.fitted_video, item.id),
                caption,
                reel.duration,
                reel.ai,
            ),
            token,
            ref,
            save_ref,
            claimed_at=claimed_at,
        )

    def permalink(self, remote_id: str) -> str:
        return self._channel.permalink(remote_id)
