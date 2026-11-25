from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, create_autospec
from uuid import uuid4

import pytest

from postpilot.channels.base import ChannelError
from postpilot.channels.fake import FakeChannel, SimulatedCrash
from postpilot.core.models import Item, Publication, PublicationStatus, Variant
from postpilot.core.publisher import (
    ChannelResult,
    Publisher,
    PublishReport,
)
from postpilot.core.states import ItemState
from postpilot.store.repo import Claim, ItemRepo
from postpilot.trace import set_secrets


def test_report_keeps_a_snapshot_of_channel_results() -> None:
    channels = {
        "first": ChannelResult("published"),
        "second": ChannelResult("failed", "An error"),
        "third": ChannelResult("skipped"),
        "fourth": ChannelResult("pending"),
    }
    report = PublishReport(channels)
    channels.clear()

    assert report.channels == {
        "first": ChannelResult("published"),
        "second": ChannelResult("failed", "An error"),
        "third": ChannelResult("skipped"),
        "fourth": ChannelResult("pending"),
    }


def test_publisher_recovers_a_send_after_a_crash_without_sending_again() -> None:
    now = datetime.now(UTC)
    item = Item(uuid4(), "github", ItemState.PUBLISHING, now, now, 1, now)
    publication = Publication(
        item.id, "fake", PublicationStatus.PUBLISHING, None, None, now
    )
    first = Claim(publication, recovered=False)
    recovered = Claim(publication, recovered=True)
    repo = create_autospec(ItemRepo, instance=True)
    repo.get_item.return_value = item
    repo.get_variants.return_value = [Variant(item.id, "de", "Ein Beitrag")]
    repo.list_publications.return_value = [publication]
    repo.claim_publication.side_effect = [first, recovered]
    channel = FakeChannel("fake")
    channel.crash_after_send = True

    with pytest.raises(SimulatedCrash):
        Publisher(repo, [channel]).publish_item(item.id)
    channel.crash_after_send = False
    repo.mark_published.assert_not_called()
    repo.mark_failed.assert_not_called()
    repo.finish_publishing.assert_not_called()
    assert len(channel.posts) == 1

    report = Publisher(repo, [channel], stale_after=timedelta(0)).publish_item(item.id)
    assert report.channels["fake"].outcome == "published"
    assert len(channel.posts) == 1
    repo.mark_published.assert_called_once_with(
        item.id, channel.name, next(iter(channel.posts)), claim=recovered
    )
    repo.finish_publishing.assert_called_once_with(
        item.id, {channel.name}, held=frozenset()
    )


def test_lost_claim_during_ref_save_leaves_publication_pending() -> None:
    from collections.abc import Callable

    from postpilot.store.repo import LostPublicationClaim

    now = datetime.now(UTC)
    item = Item(uuid4(), "github", ItemState.PUBLISHING, now, now, 1, now)
    variants = [Variant(item.id, "de", "Ein Beitrag")]
    claim = Claim(
        Publication(
            item.id, "container", PublicationStatus.PUBLISHING, None, None, now
        ),
        recovered=False,
    )
    repo = create_autospec(ItemRepo, instance=True)
    repo.get_item.return_value = item
    repo.get_variants.return_value = variants
    repo.list_publications.return_value = [claim.publication]
    repo.claim_publication.return_value = claim
    repo.save_publication_ref.side_effect = LostPublicationClaim

    class ContainerChannel:
        name = "container"

        def validate(self, item: Item, variants: list[Variant]) -> None:
            pass

        def publish_with_ref(
            self,
            item: Item,
            variants: list[Variant],
            *,
            ref: str | None,
            claimed_at: datetime,
            save_ref: Callable[[str], None],
        ) -> str:
            assert ref is None
            assert claimed_at == now
            save_ref("container-1")
            pytest.fail("The channel must stop before sending after losing its claim")

    report = Publisher(repo, [ContainerChannel()]).publish_item(item.id)

    assert report.channels["container"] == ChannelResult("pending")
    repo.save_publication_ref.assert_called_once_with(claim, "container-1")
    repo.mark_published.assert_not_called()
    repo.mark_failed.assert_not_called()
    repo.release_publication.assert_not_called()


def test_publisher_traces_claim_retry_and_result_without_text(capsys) -> None:
    now = datetime.now(UTC)
    item = Item(uuid4(), "github", ItemState.APPROVED, now, now, 1, None)
    publication = Publication(
        item.id, "fake", PublicationStatus.PUBLISHING, None, None, now
    )
    repo = create_autospec(ItemRepo, instance=True)
    repo.get_item.return_value = item
    repo.transition.return_value = item
    repo.get_variants.return_value = [Variant(item.id, "de", "Geheimer Beitragstext")]
    repo.list_publications.return_value = []
    repo.claim_publication.return_value = Claim(publication, recovered=False)
    channel = FakeChannel("fake")
    channel.fail_times = 1

    report = Publisher(repo, [channel], sleep=lambda _: None).publish_item(item.id)

    assert report.channels["fake"].outcome == "published"
    out = capsys.readouterr().out
    events = [line.split()[2] for line in out.splitlines()]
    assert events == ["item", "claim", "transient", "remote", "channel", "finished"]
    # Fake remote ids are UUID strings, which the token-shape redaction hides.
    assert "channel=fake remote_id=***" in out
    assert (
        "attempt=1 error=TransientChannelError "
        "reason=TransientChannelError:_Transient_fake_channel_failure"
    ) in out
    assert "Beitragstext" not in out


@pytest.mark.parametrize("source", ["own_video", "github", "telegram"])
def test_selects_adapters_for_item_source(source):
    from dataclasses import replace

    now = datetime.now(UTC)
    item = Item(uuid4(), source, ItemState.APPROVED, now, now, 1, now)
    variants = [Variant(item.id, "de", "Ein Beitrag")]
    repo = create_autospec(ItemRepo, instance=True)
    repo.get_item.return_value = item
    repo.get_variants.return_value = variants
    repo.list_publications.return_value = []
    names = ["linkedin", "threads"]
    reels = ("instagram-reels", "facebook-reels")
    base = [FakeChannel(name) for name in names]
    video = [FakeChannel(name) for name in names[:1]]
    statuses = {}

    def transition(item_id, state):
        assert item_id == item.id
        repo.get_item.return_value = replace(item, state=state)
        return repo.get_item.return_value

    def claim(item_id, channel, *, stale_after):
        assert repo.get_item.return_value.state == ItemState.PUBLISHING
        return Claim(
            Publication(
                item_id, channel, PublicationStatus.PUBLISHING, None, None, now
            ),
            recovered=False,
        )

    def mark_published(item_id, name, remote_id, *, claim):
        statuses[name] = remote_id

    def finish(item_id, channels, *, held):
        if channels <= statuses.keys():
            transition(item_id, ItemState.PUBLISHED)

    repo.transition.side_effect = transition
    repo.claim_publication.side_effect = claim
    repo.mark_published.side_effect = mark_published
    repo.finish_publishing.side_effect = finish
    report = Publisher(
        repo, base, own_video_channels=video, own_video_held_channels=reels
    ).publish_item(item.id)

    if source == "own_video":
        assert [result.outcome for result in report.channels.values()] == [
            "published",
            "held",
            "held",
            "held",
        ]
        assert set(statuses) == set(names[:1])
        assert repo.claim_publication.call_count == 1
        assert repo.get_item.return_value.state == ItemState.PUBLISHING
        assert all(channel.posts for channel in video)
        assert not any(channel.posts for channel in base)
    else:
        assert all(result.outcome == "published" for result in report.channels.values())
        assert repo.get_item.return_value.state == ItemState.PUBLISHED
        assert all(channel.posts for channel in base)
        assert not any(channel.posts for channel in video)
    expected = set(names) | (set(reels) if source == "own_video" else set())
    repo.finish_publishing.assert_called_once_with(
        item.id,
        expected,
        held=frozenset(
            name for name, result in report.channels.items() if result.outcome == "held"
        ),
    )


def test_own_video_without_adapters_is_held_before_transition():
    now = datetime.now(UTC)
    item = Item(uuid4(), "own_video", ItemState.APPROVED, now, now, 1, now)
    repo = create_autospec(ItemRepo, instance=True)
    repo.get_item.return_value = item
    report = Publisher(repo, [FakeChannel("linkedin")]).publish_item(item.id)
    assert report.channels == {"linkedin": ChannelResult("held")}
    repo.transition.assert_not_called()
    repo.claim_publication.assert_not_called()
    repo.finish_publishing.assert_not_called()


def test_ref_adapter_waiting_for_video_reports_pending():
    from unittest.mock import Mock

    now = datetime.now(UTC)
    item = Item(uuid4(), "own_video", ItemState.PUBLISHING, now, now, 1, now)
    repo = create_autospec(ItemRepo, instance=True)
    repo.get_item.return_value = item
    repo.get_variants.return_value = []
    repo.list_publications.return_value = []
    claim = Claim(
        Publication(item.id, "linkedin", PublicationStatus.PUBLISHING, None, None, now),
        recovered=False,
    )
    repo.claim_publication.return_value = claim
    channel = Mock(validate=Mock(), publish_with_ref=Mock(return_value=None))
    channel.name = "linkedin"
    publisher = Publisher(repo, [FakeChannel("linkedin")], own_video_channels=[channel])

    assert publisher.publish_item(item.id).channels == {
        "linkedin": ChannelResult("pending")
    }
    repo.mark_published.assert_not_called()
    repo.mark_failed.assert_not_called()


def test_publisher_stores_and_traces_one_sanitized_failure(capsys):
    now = datetime.now(UTC)
    item = Item(uuid4(), "github", ItemState.PUBLISHING, now, now, 1, now)
    publication = Publication(
        item.id, "fake", PublicationStatus.PUBLISHING, None, None, now
    )
    claim = Claim(publication, recovered=False)
    repo = create_autospec(ItemRepo, instance=True)
    repo.get_item.return_value = item
    repo.get_variants.return_value = [Variant(item.id, "de", "Ein Beitrag")]
    repo.list_publications.return_value = [publication]
    repo.claim_publication.return_value = claim
    channel = create_autospec(FakeChannel("fake"), instance=True)
    channel.name = "fake"
    description = (
        "Instagram request rejected while publishing the approved video (HTTP 400)"
    )
    secret = "example-sensitive-value"
    channel.publish = Mock(side_effect=ChannelError(f"{description} {secret}"))
    set_secrets([secret])
    try:
        report = Publisher(repo, [channel]).publish_item(item.id)
    finally:
        set_secrets(())

    reason = f"ChannelError: {description} ***"
    assert report.channels["fake"] == ChannelResult("failed", reason)
    repo.mark_failed.assert_called_once_with(item.id, "fake", reason, claim=claim)
    out = capsys.readouterr().out
    assert secret not in out
    assert [
        line for line in out.splitlines() if line.startswith("trace publish failed")
    ] == [
        f"trace publish failed item={item.id} channel=fake reason={reason.replace(' ', '_')}"
    ]
