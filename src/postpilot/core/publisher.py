import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import Literal
from uuid import UUID

from postpilot.channels.base import (
    Channel,
    ChannelHeld,
    RefChannel,
    TransientChannelError,
    UncertainPublication,
)
from postpilot.core.models import Item, PublicationStatus, Variant
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.store.repo import Claim, ItemRepo, LostPublicationClaim
from postpilot.trace import error_fields, trace

type Outcome = Literal["published", "skipped", "failed", "pending", "held"]


@dataclass(frozen=True)
class ChannelResult:
    outcome: Outcome
    error: str | None = None


@dataclass(frozen=True)
class PublishReport:
    channels: Mapping[str, ChannelResult]

    def __post_init__(self) -> None:
        object.__setattr__(self, "channels", MappingProxyType(dict(self.channels)))


def _redact_error(error: Exception) -> str:
    # Adapter exception messages can contain credentials or entire request bodies.
    return f"Channel operation failed ({type(error).__name__})"[:200]


class Publisher:
    def __init__(
        self,
        repo: ItemRepo,
        channels: list[Channel],
        stale_after: timedelta = timedelta(minutes=15),
        *,
        sleep: Callable[[float], None] = time.sleep,
        token_guard: Callable[[str], bool] | None = None,
        own_video_channels: list[Channel] | None = None,
        own_video_held_channels: tuple[str, ...] = (),
    ) -> None:
        self.repo = repo
        self.channels = channels
        self.stale_after = stale_after
        self.sleep = sleep
        self.token_guard = token_guard
        self.own_video_channels = own_video_channels or []
        self.own_video_held_channels = own_video_held_channels

    def publish_item(self, item_id: UUID) -> PublishReport:
        if not self.channels:
            raise ValueError("No channels configured")
        item = self.repo.get_item(item_id)
        if item is None:
            raise KeyError(item_id)
        own_video = item.source == "own_video"
        if own_video and not self.own_video_channels:
            return PublishReport(
                {channel.name: ChannelResult("held") for channel in self.channels}
            )
        video_adapters = {channel.name: channel for channel in self.own_video_channels}
        if item.state not in {
            ItemState.APPROVED,
            ItemState.FAILED,
            ItemState.PUBLISHING,
        }:
            raise IllegalTransition(f"Cannot publish an item in {item.state}")
        trace(
            "publish",
            "item",
            item=item.id,
            source=item.source,
            state=item.state,
            channels=[channel.name for channel in self.channels],
        )
        if item.state != ItemState.PUBLISHING:
            try:
                item = self.repo.transition(item_id, ItemState.PUBLISHING)
            except IllegalTransition:
                current = self.repo.get_item(item_id)
                if current is None or current.state != ItemState.PUBLISHING:
                    raise
                item = current

        variants = self.repo.get_variants(item_id)
        published = {
            publication.channel
            for publication in self.repo.list_publications(item_id)
            if publication.status == PublicationStatus.PUBLISHED
        }
        results = {}
        for channel in self.channels:
            name = channel.name
            adapter = video_adapters.get(name) if own_video else channel
            if adapter is None:
                results[name] = ChannelResult("held")
            elif name in published:
                results[name] = ChannelResult("skipped")
            else:
                results[name] = self._publish_channel(adapter, item, variants)
        if own_video:
            for name in self.own_video_held_channels:
                results.setdefault(name, ChannelResult("held"))
        for name, result in results.items():
            trace(
                "publish",
                "channel",
                item=item_id,
                channel=name,
                outcome=result.outcome,
                error=result.error,
            )
        self.repo.finish_publishing(item_id, set(results))
        finished = self.repo.get_item(item_id)
        trace("publish", "finished", item=item_id, state=finished and finished.state)
        return PublishReport(results)

    def _publish_channel(
        self, channel: Channel, item: Item, variants: list[Variant]
    ) -> ChannelResult:
        provider = getattr(channel, "token_provider", channel.name)
        if self.token_guard is not None and not self.token_guard(provider):
            return ChannelResult("held")

        validation_error = None
        try:
            channel.validate(item, variants)
        except Exception as error:  # noqa: BLE001 - isolate adapter failures
            validation_error = _redact_error(error)

        try:
            claim = self.repo.claim_publication(
                item.id, channel.name, stale_after=self.stale_after
            )
        except IllegalTransition:
            # Another worker finalized the item while this adapter was validating.
            return ChannelResult("pending")
        if claim is None:
            return ChannelResult("skipped")
        trace(
            "publish",
            "claim",
            item=item.id,
            channel=channel.name,
            stale=claim.stale,
            recovered=claim.recovered,
            has_ref=claim.remote_ref is not None,
            validation_failed=validation_error is not None,
        )

        recover_only = getattr(channel, "recover_only", False)
        preserve_claim = (
            recover_only
            and claim.stale
            and (claim.remote_ref is not None or not isinstance(channel, RefChannel))
        )
        if validation_error is None or preserve_claim:
            try:
                remote_id = self._send(channel, item, variants, claim)
            except ChannelHeld:
                if not preserve_claim:
                    self.repo.release_publication(claim)
                return ChannelResult("held")
            except LostPublicationClaim:
                return ChannelResult("pending")
            except UncertainPublication as error:
                return ChannelResult("pending", _redact_error(error))
            except Exception as error:  # noqa: BLE001 - isolate adapter failures
                if preserve_claim:
                    return ChannelResult("pending", _redact_error(error))
                validation_error = _redact_error(error)
            else:
                if remote_id is None:
                    return ChannelResult("pending")
                try:
                    self.repo.mark_published(
                        item.id, channel.name, remote_id, claim=claim
                    )
                except LostPublicationClaim:
                    return ChannelResult("pending")
                trace(
                    "publish",
                    "remote",
                    item=item.id,
                    channel=channel.name,
                    remote_id=remote_id,
                )
                return ChannelResult("published")

        try:
            self.repo.mark_failed(item.id, channel.name, validation_error, claim=claim)
        except LostPublicationClaim:
            return ChannelResult("pending")
        return ChannelResult("failed", validation_error)

    def _send(
        self, channel: Channel, item: Item, variants: list[Variant], claim: Claim
    ) -> str | None:
        recover_only = getattr(channel, "recover_only", False)
        if not isinstance(channel, RefChannel) and recover_only and claim.stale:
            return channel.find_existing(item.id)

        ref = claim.remote_ref

        def save_ref(new_ref: str) -> None:
            nonlocal ref
            self.repo.save_publication_ref(claim, new_ref)
            if item.source == "own_video":
                ref = new_ref

        recover = claim.recovered and not recover_only
        for attempt in range(3):
            try:
                if isinstance(channel, RefChannel):
                    return channel.publish_with_ref(
                        item,
                        variants,
                        ref=ref,
                        claimed_at=claim.publication.remote_ref_claimed_at
                        or claim.publication.updated_at,
                        save_ref=save_ref,
                    )
                if recover:
                    existing = channel.find_existing(item.id)
                    if existing is not None:
                        return existing
                return channel.publish(item, variants)
            except ChannelHeld:
                if item.source == "own_video" and ref is not None:
                    return None
                raise
            except UncertainPublication:
                raise
            except TransientChannelError as error:
                trace(
                    "publish",
                    "transient",
                    item=item.id,
                    channel=channel.name,
                    attempt=attempt + 1,
                    **error_fields(error),
                )
                if attempt == 2:
                    raise
                recover = not recover_only
                self.sleep(float(2**attempt))
        raise AssertionError("Unreachable retry state")
