from collections.abc import Callable
from datetime import datetime
from functools import partial
from hashlib import sha256
from urllib.parse import quote
from uuid import UUID

import httpx

from postpilot.channels.base import (
    ChannelError,
    ChannelHeld,
    TransientChannelError,
    UncertainPublication,
)
from postpilot.channels.linkedin import LinkedInChannel
from postpilot.channels.linkedin_video import LinkedInVideoChannel
from postpilot.channels.own_video_reels import _fit_video
from postpilot.channels.threads import ThreadsChannel
from postpilot.core.models import Item, Variant
from postpilot.projects import Projects
from postpilot.sources.factory import FactoryRelease, Rejected
from postpilot.store.project_items import Asset, ProjectItem, ProjectItemStore
from postpilot.video.label import AI_VIDEO_CAPTION_LABEL
from postpilot.video.publisher import VideoChannel
from postpilot.video.reel import ReelVideo

_VIDEO = "reel_de.mp4"
_COVER = "cover_de.png"
_TEXT_POSTING = "text-posting"
_TEXT_READY = "text-ready"


def _asset_name(reel: ProjectItem, current: str, legacy: str) -> str:
    return current if current in reel.assets else legacy


class FactoryAssets:
    def __init__(
        self, store: ProjectItemStore, client: httpx.Client, projects: Projects
    ) -> None:
        self.store = store
        self.client = client
        self.projects = projects
        # Keep the release and downloaded media for the current item only.
        self._key: tuple[UUID, str, dict[str, Asset]] | None = None
        self._release: FactoryRelease | None = None
        self._downloads: dict[str, bytes] = {}
        self._errors: dict[str, str] = {}
        self._fitted: bytes | None = None

    def item(self, item_id: UUID) -> ProjectItem:
        item = self.store.get_by_item(item_id)
        if item is None:
            raise ChannelError("Project item data is unavailable")
        return item

    def reel(self, item_id: UUID) -> ProjectItem:
        reel = self.item(item_id)
        if reel.kind != "reel":
            raise ChannelError("Factory reel data is unavailable")
        return reel

    def _release_for(self, item_id: UUID, reel: ProjectItem) -> FactoryRelease:
        key = (item_id, reel.tag, reel.assets)
        if self._key != key:
            self._key = key
            self._release = None
            self._downloads.clear()
            self._errors.clear()
            self._fitted = None
        if self._release is None and "release" not in self._errors:
            try:
                project = self.projects.get(reel.project)
                if project is None:
                    raise ChannelError("Project release repository is unavailable")
                self._release = FactoryRelease(self.client, project.repo, reel.tag)
            except Rejected:
                self._errors["release"] = f"Factory release {reel.tag} is unavailable"
        if self._release is None:
            raise ChannelError(self._errors["release"])
        return self._release

    def check_release(self, item_id: UUID) -> None:
        reel = self.item(item_id)
        if reel.kind == "post":
            self._download(item_id, "post_de.txt")
            if "post_threads.txt" in reel.assets:
                self._download(item_id, "post_threads.txt")
            return
        release = self._release_for(item_id, reel)
        name = _asset_name(reel, "video.mp4", _VIDEO)
        if name in self._errors:
            raise ChannelError(self._errors[name])
        try:
            release.asset_url(name)
        except Rejected as error:
            raise ChannelError(f"Factory {error}") from None

    def _download(self, item_id: UUID, name: str) -> bytes:
        reel = self.item(item_id)
        release = self._release_for(item_id, reel)
        if name not in self._downloads and name not in self._errors:
            try:
                data = release.download(name, release.asset_url(name))
                if reel.assets.get(name) != {
                    "size": len(data),
                    "sha256": sha256(data).hexdigest(),
                }:
                    raise ChannelError(f"Factory asset {name} changed since intake")
                if name in {_COVER, "cover.png"} and not data.startswith(
                    b"\x89PNG\r\n\x1a\n"
                ):
                    raise ChannelError(
                        f"Factory asset {name} must have a PNG signature"
                    )
                self._downloads[name] = data
            except Rejected as error:
                self._errors[name] = f"Factory {error}"
            except ChannelError as error:
                self._errors[name] = str(error)
        if name in self._errors:
            raise ChannelError(self._errors[name])
        return self._downloads[name]

    def video(self, item_id: UUID) -> bytes:
        return self._download(
            item_id, _asset_name(self.reel(item_id), "video.mp4", _VIDEO)
        )

    def cover(self, item_id: UUID) -> bytes:
        return self._download(
            item_id, _asset_name(self.reel(item_id), "cover.png", _COVER)
        )

    def cover_url(self, item_id: UUID) -> str:
        reel = self.reel(item_id)
        release = self._release_for(item_id, reel)
        try:
            return str(release.asset_url(_asset_name(reel, "cover.png", _COVER)))
        except Rejected as error:
            raise ChannelError(f"Factory {error}") from None

    def fitted_video(self, item_id: UUID) -> bytes:
        raw = self.video(item_id)
        self.cover(item_id)
        if self._fitted is None:
            # Intake stores duration only. Keep the release picture and normalize audio.
            self._fitted = _fit_video(raw, scale=False)
        return self._fitted


class FactoryLinkedInChannel(LinkedInVideoChannel):
    _verbatim = True
    _file_limit = None

    def __init__(self, linkedin: LinkedInChannel, assets: FactoryAssets) -> None:
        super().__init__(
            linkedin,
            fetch_video=assets.video,
            ai_generated=lambda _: False,
            fetch_thumbnail=assets.cover,
        )
        self._assets = assets

    def _commentary(self, item: Item, variants: list[Variant]) -> str:
        reel = self._assets.item(item.id)
        return self._linkedin._commentary(
            item,
            [Variant(item.id, "de", reel.post_de)],
            label=AI_VIDEO_CAPTION_LABEL if reel.ai and reel.kind == "reel" else "",
            include_marker=False,
        )

    def publish_with_ref(
        self,
        item: Item,
        variants: list[Variant],
        *,
        ref: str | None,
        claimed_at: datetime,
        save_ref: Callable[[str], None],
    ) -> str | None:
        if self._assets.item(item.id).kind != "post":
            return super().publish_with_ref(
                item, variants, ref=ref, claimed_at=claimed_at, save_ref=save_ref
            )

        linkedin = self._linkedin
        token = linkedin._token()
        text = self._commentary(item, variants)
        if ref == _TEXT_POSTING:
            try:
                return linkedin._find_text_post(
                    text, token, int(claimed_at.timestamp() * 1000)
                )
            except ChannelError:
                raise UncertainPublication("LinkedIn post lookup failed") from None
        if ref not in {None, _TEXT_READY}:
            raise ChannelError("Invalid LinkedIn text publication reference")

        self._assets.check_release(item.id)
        author = linkedin._author(token)
        save_ref(_TEXT_POSTING)
        try:
            return linkedin._create_post(text, author, token)
        except UncertainPublication:
            raise
        except TransientChannelError:
            save_ref(_TEXT_READY)
            return None
        except (ChannelError, ChannelHeld):
            save_ref(_TEXT_READY)
            raise


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
        text = self._assets.item(item.id).post_threads
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
        use_release_video: bool = False,
    ) -> None:
        self.name = channel.name
        self.token_provider = channel.token_provider
        self._channel = channel
        self._read_token = read_token
        self._assets = assets
        self._caption_limit = caption_limit
        self._use_release_video = use_release_video

    def _caption(self, reel: ProjectItem) -> str:
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
        if reel.duration is None:
            raise ChannelError("Reel duration is unavailable")
        try:
            cover_url = (
                None if self._use_release_video else self._assets.cover_url(item.id)
            )
        except ChannelError:
            if ref is None:
                raise
            # Let Meta reconcile an existing publication even if the release is gone.
            # Any path that needs the video will validate the cover before publishing.
            cover_url = None
        return self._channel.publish_with_ref(
            ReelVideo(
                partial(
                    self._assets.video
                    if self._use_release_video
                    else self._assets.fitted_video,
                    item.id,
                ),
                caption,
                reel.duration,
                reel.ai,
                cover=partial(self._assets.cover, item.id),
                cover_url=cover_url,
                title=reel.title_de,
            ),
            token,
            ref,
            save_ref,
            claimed_at=claimed_at,
        )

    def permalink(self, remote_id: str) -> str:
        return self._channel.permalink(remote_id)
