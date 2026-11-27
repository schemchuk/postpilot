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
    ConfirmedPublicationFailure,
    RefChannel,
    TransientChannelError,
    UncertainPublication,
)
from postpilot.channels.paused import PAUSED_CHANNELS
from postpilot.core.approval import (
    ProjectApprovalError,
    ProjectDailyLimitError,
    auto_approved_project,
    enabled_project,
)
from postpilot.core.models import Item, PublicationStatus, Variant
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.projects import CHANNEL_NAMES, POST_CHANNELS, Projects
from postpilot.store.project_items import ProjectItem, ProjectItemStore
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
        project_channels: list[Channel] | None = None,
        project_item: Callable[[UUID], ProjectItem | None] | None = None,
        projects: Projects | None = None,
        project_exclusions: Callable[[ProjectItem], set[str]] | None = None,
        project_lock: Callable[[UUID], AbstractContextManager[None]] | None = None,
        project_published_today: Callable[[str, datetime], int] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.project_channels = project_channels
        self.project_item = project_item
        self.projects = projects
        self.project_exclusions = project_exclusions
        self.project_lock = project_lock
        self.project_published_today = project_published_today
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
        if (
            not (self.channels or self.own_video_channels)
            and self.project_channels is None
        ):
            raise ValueError("No channels configured")
        item = self.repo.get_item(item_id)
        if item is None:
            raise KeyError(item_id)
        project = item.source == "project"
        own_video = item.source == "own_video"
        lock = (
            partial(self.project_lock, item_id)
            if project and self.project_lock is not None
            else nullcontext
        )
        with lock():
            scheduled = None
            if project:
                if (
                    self.project_channels is None
                    or self.project_item is None
                    or self.projects is None
                ):
                    raise IllegalTransition(
                        "Project items require the project publisher"
                    )
                item = self.repo.get_item(item_id)
                if item is None:
                    raise KeyError(item_id)
                scheduled = self.project_item(item_id)
                if scheduled is None:
                    raise IllegalTransition("Project item data is unavailable")
                if scheduled.approval == "auto":
                    policy = auto_approved_project(self.projects, scheduled.project)
                else:
                    policy = enabled_project(self.projects, scheduled.project)
                    if item.approved_by is None:
                        raise ProjectApprovalError(
                            f"Project {policy.key!r} needs a tap"
                        )
                if (
                    scheduled.kind not in {"reel", "post"}
                    or scheduled.kind not in policy.kinds
                ):
                    raise ProjectApprovalError(
                        f"Project {policy.key!r} cannot publish {scheduled.kind}"
                    )
                now = self.now()
                if scheduled.release_at > now or item.state in {
                    ItemState.FAILED,
                    ItemState.SKIPPED,
                }:
                    return PublishReport({})
                channels = self.project_channels
                allowed = {
                    name
                    for logical in policy.channels
                    if (scheduled.channels is None or logical in scheduled.channels)
                    and (
                        scheduled.kind != "post"
                        or (
                            logical in POST_CHANNELS
                            and not (logical == "threads" and scheduled.threads_skipped)
                        )
                    )
                    for name in CHANNEL_NAMES[logical]
                }
                excluded = set(PAUSED_CHANNELS) | {
                    channel.name for channel in channels if channel.name not in allowed
                }
                if self.project_exclusions is not None:
                    excluded.update(self.project_exclusions(scheduled))
                if all(channel.name in excluded for channel in channels):
                    return PublishReport({})
                if item.state == ItemState.APPROVED:
                    published_today = (
                        self.project_published_today
                        or ProjectItemStore(self.repo.conn).published_today
                    )
                    if published_today(policy.key, now) >= policy.max_per_day:
                        raise ProjectDailyLimitError(
                            f"Project {policy.key} daily limit {policy.max_per_day} reached; "
                            f"{scheduled.tag} waits until tomorrow"
                        )
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
                # Recheck the project item under the same item lock as the claim.
                if (
                    scheduled is not None
                    and self.project_item is not None
                    and self.project_item(item_id) != scheduled
                ):
                    raise IllegalTransition("Project item changed during publishing")
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
            ).union(excluded if project else ()),
        )
        finished = self.repo.get_item(item_id)
        trace("publish", "finished", item=item_id, state=finished and finished.state)
        return PublishReport(results, transitioned_to_failed)

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
            # The item or its project schedule changed before the claim.
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
            except ConfirmedPublicationFailure as error:
                validation_error = describe_error(error)
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
            if item.source in {"own_video", "project"}:
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
                if item.source in {"own_video", "project"} and ref is not None:
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
