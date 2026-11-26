import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
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
from postpilot.channels.paused import PAUSED_CHANNELS
from postpilot.core.models import Item, PublicationStatus, Variant
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.store.repo import Claim, ItemRepo, LostPublicationClaim
from postpilot.trace import describe_error, error_fields, trace

type Outcome = Literal["published", "skipped", "failed", "pending", "held"]


@dataclass(frozen=True)
class ChannelResult:
    outcome: Outcome
    error: str | None = None


@dataclass(frozen=True)
class PublishReport:
    channels: Mapping[str, ChannelResult]
    transitioned_to_failed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "channels", MappingProxyType(dict(self.channels)))


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
        own_video_excluded_channels: Callable[[UUID], set[str]] | None = None,
        factory_channels: list[Channel] | None = None,
        factory_release_at: Callable[[UUID], datetime | None] | None = None,
        factory_excluded_channels: Callable[[UUID], set[str]] | None = None,
        factory_lock: Callable[[UUID], AbstractContextManager[None]] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.factory_channels = factory_channels
        self.factory_release_at = factory_release_at
        self.factory_excluded_channels = factory_excluded_channels
        self.factory_lock = factory_lock
        self.now = now
        self.repo = repo
        self.channels = channels
        self.stale_after = stale_after
        self.sleep = sleep
        self.token_guard = token_guard
        self.own_video_channels = own_video_channels
        self.own_video_held_channels = own_video_held_channels
        self.own_video_excluded_channels = own_video_excluded_channels

    def publish_item(self, item_id: UUID) -> PublishReport:
        if not (self.channels or self.own_video_channels or self.factory_channels):
            raise ValueError("No channels configured")
        item = self.repo.get_item(item_id)
        if item is None:
            raise KeyError(item_id)
        factory = item.source == "factory"
        own_video = item.source == "own_video"
        lock = (
            partial(self.factory_lock, item_id)
            if factory and self.factory_lock is not None
            else nullcontext
        )
        with lock():
            if factory:
                if not self.factory_channels or self.factory_release_at is None:
                    raise IllegalTransition(
                        "Factory reels require the factory publisher"
                    )
                item = self.repo.get_item(item_id)
                if item is None:
                    raise KeyError(item_id)
                release_at = self.factory_release_at(item_id)
                if (
                    release_at is None
                    or release_at > self.now()
                    or item.state in {ItemState.FAILED, ItemState.SKIPPED}
                ):
                    return PublishReport({})
                channels = self.factory_channels
                excluded = self._factory_exclusions(item_id)
                if all(channel.name in excluded for channel in channels):
                    return PublishReport({})
            elif own_video:
                if self.own_video_channels is None:
                    return PublishReport(
                        {
                            channel.name: ChannelResult("held")
                            for channel in self.channels
                        }
                    )
                channels = self.own_video_channels
                excluded = (
                    self.own_video_excluded_channels(item_id)
                    if self.own_video_excluded_channels is not None
                    else set()
                )
            else:
                channels, excluded = self.channels, set()
                if not channels:
                    raise ValueError("No channels configured")
            if item.state not in {
                ItemState.APPROVED,
                ItemState.FAILED,
                ItemState.PUBLISHING,
            }:
                raise IllegalTransition(f"Cannot publish an item in {item.state}")
            if item.state != ItemState.PUBLISHING:
                try:
                    item = self.repo.transition(item_id, ItemState.PUBLISHING)
                except IllegalTransition:
                    current = self.repo.get_item(item_id)
                    if current is None or current.state != ItemState.PUBLISHING:
                        raise
                    item = current

        def claim_publication(name: str) -> Claim | None:
            with lock():
                # Intake can replace an item until a channel holds a claim.
                # Recheck the schedule under the same item lock as the claim.
                if (
                    factory
                    and self.factory_release_at is not None
                    and (
                        self.factory_release_at(item_id) != release_at
                        or self._factory_exclusions(item_id) != excluded
                    )
                ):
                    raise IllegalTransition(
                        "Factory schedule changed during publishing"
                    )
                return self.repo.claim_publication(
                    item_id, name, stale_after=self.stale_after
                )

        trace(
            "publish",
            "item",
            item=item.id,
            source=item.source,
            state=item.state,
            channels=[
                channel.name for channel in channels if channel.name not in excluded
            ],
        )
        variants = self.repo.get_variants(item_id)
        published = {
            publication.channel
            for publication in self.repo.list_publications(item_id)
            if publication.status == PublicationStatus.PUBLISHED
        }
        results = {}
        for channel in channels:
            name = channel.name
            if name in excluded:
                continue
            if name in published:
                results[name] = ChannelResult("skipped")
            else:
                results[name] = self._publish_channel(
                    channel, item, variants, claim_publication
                )
        if own_video:
            for name in (
                *[channel.name for channel in self.channels],
                *self.own_video_held_channels,
            ):
                if name not in excluded:
                    results.setdefault(
                        name, ChannelResult("skipped" if name in published else "held")
                    )
        for name, result in results.items():
            trace(
                "publish",
                "channel",
                item=item_id,
                channel=name,
                outcome=result.outcome,
                error=result.error,
            )
        transitioned_to_failed = self.repo.finish_publishing(
            item_id,
            set(results),
            held=frozenset(
                name for name, result in results.items() if result.outcome == "held"
            ).union(excluded if factory else ()),
        )
        finished = self.repo.get_item(item_id)
        trace("publish", "finished", item=item_id, state=finished and finished.state)
        return PublishReport(results, transitioned_to_failed)

    def _factory_exclusions(self, item_id: UUID) -> set[str]:
        excluded = set(PAUSED_CHANNELS)
        if self.factory_excluded_channels is not None:
            excluded.update(self.factory_excluded_channels(item_id))
        return excluded

    def _publish_channel(
        self,
        channel: Channel,
        item: Item,
        variants: list[Variant],
        claim_publication: Callable[[str], Claim | None],
    ) -> ChannelResult:
        provider = getattr(channel, "token_provider", channel.name)
        if self.token_guard is not None and not self.token_guard(provider):
            return ChannelResult("held")

        try:
            claim = claim_publication(channel.name)
        except IllegalTransition:
            # The item or its factory schedule changed before the claim.
            return ChannelResult("pending")
        if claim is None:
            return ChannelResult("skipped")
        validation_error = None
        try:
            channel.validate(item, variants)
        except Exception as error:  # noqa: BLE001 - isolate adapter failures
            validation_error = describe_error(error)

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
                return ChannelResult("pending", describe_error(error))
            except Exception as error:  # noqa: BLE001 - isolate adapter failures
                if preserve_claim:
                    return ChannelResult("pending", describe_error(error))
                validation_error = describe_error(error)
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
        trace(
            "publish",
            "failed",
            item=item.id,
            channel=channel.name,
            reason=validation_error,
        )
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
            if item.source in {"own_video", "factory"}:
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
                if item.source in {"own_video", "factory"} and ref is not None:
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
                    reason=describe_error(error),
                )
                if attempt == 2:
                    raise
                recover = not recover_only
                self.sleep(float(2**attempt))
        raise AssertionError("Unreachable retry state")
