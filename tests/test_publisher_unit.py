from datetime import UTC, datetime, timedelta
from unittest.mock import create_autospec
from uuid import uuid4

import pytest

from postpilot.channels.fake import FakeChannel, SimulatedCrash
from postpilot.core.models import Item, Publication, PublicationStatus, Variant
from postpilot.core.publisher import (
    ChannelResult,
    Publisher,
    PublishReport,
    _redact_error,
)
from postpilot.core.states import ItemState
from postpilot.store.repo import Claim, ItemRepo


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


def test_error_does_not_expose_adapter_message() -> None:
    error = ValueError("Authorization: Bearer secret-token")
    assert _redact_error(error) == "Channel operation failed (ValueError)"


def test_error_text_is_capped_at_200_characters() -> None:
    long_exception = type("LongError" * 40, (Exception,), {})
    assert len(_redact_error(long_exception())) == 200


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
    repo.finish_publishing.assert_called_once_with(item.id, {channel.name})


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
    assert "attempt=1 error=TransientChannelError" in out
    assert "Beitragstext" not in out
