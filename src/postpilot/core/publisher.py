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
    TransientChannelError,
    UncertainPublication,
)
from postpilot.core.models import Item, PublicationStatus, Variant
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.store.repo import ItemRepo, LostPublicationClaim

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
    ) -> None:
        self.repo = repo
        self.channels = channels
        self.stale_after = stale_after
        self.sleep = sleep
        self.token_guard = token_guard

    def publish_item(self, item_id: UUID) -> PublishReport:
        if not self.channels:
            raise ValueError("No channels configured")
        item = self.repo.get_item(item_id)
        if item is None:
            raise KeyError(item_id)
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

        variants = self.repo.get_variants(item_id)
        published = {
            publication.channel
            for publication in self.repo.list_publications(item_id)
            if publication.status == PublicationStatus.PUBLISHED
        }
        results = {
            channel.name: (
                ChannelResult("skipped")
                if channel.name in published
                else self._publish_channel(channel, item, variants)
            )
            for channel in self.channels
        }
        self.repo.finish_publishing(item_id, set(results))
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

        recover_only = getattr(channel, "recover_only", False)
        lookup_only = recover_only and claim.stale
        if validation_error is None or lookup_only:
            try:
                remote_id = (
                    channel.find_existing(item.id)
                    if lookup_only
                    else self._send(
                        channel, item, variants, claim.recovered and not recover_only
                    )
                )
            except ChannelHeld:
                if not lookup_only:
                    self.repo.release_publication(claim)
                return ChannelResult("held")
            except UncertainPublication as error:
                return ChannelResult("pending", _redact_error(error))
            except Exception as error:  # noqa: BLE001 - isolate adapter failures
                if lookup_only:
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
                return ChannelResult("published")

        try:
            self.repo.mark_failed(item.id, channel.name, validation_error, claim=claim)
        except LostPublicationClaim:
            return ChannelResult("pending")
        return ChannelResult("failed", validation_error)

    def _send(
        self, channel: Channel, item: Item, variants: list[Variant], recover: bool
    ) -> str:
        for attempt in range(3):
            try:
                if recover:
                    existing = channel.find_existing(item.id)
                    if existing is not None:
                        return existing
                return channel.publish(item, variants)
            except UncertainPublication:
                raise
            except TransientChannelError:
                if attempt == 2:
                    raise
                recover = not getattr(channel, "recover_only", False)
                self.sleep(float(2**attempt))
        raise AssertionError("Unreachable retry state")
