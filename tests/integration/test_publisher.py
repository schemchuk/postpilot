from datetime import timedelta

import pytest
from test_store import approved_item, conn, database_schema, pytestmark, repo

from postpilot.channels.fake import FakeChannel, SimulatedCrash
from postpilot.core.models import PublicationStatus
from postpilot.core.publisher import Publisher
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.store.repo import LostPublicationClaim

# Make the shared fixtures available to pytest in this module.
__all__ = ["approved_item", "conn", "database_schema", "pytestmark", "repo"]


@pytest.fixture
def ready_item(repo, approved_item):
    repo.add_variant(approved_item.id, "de", "Ein Beitrag")
    return approved_item


def test_published_item_cannot_send_again(repo, ready_item):
    channel = FakeChannel("fake")
    publisher = Publisher(repo, [channel])
    assert publisher.publish_item(ready_item.id).channels["fake"].outcome == "published"
    with pytest.raises(IllegalTransition):
        publisher.publish_item(ready_item.id)
    assert len(channel.posts) == 1


def test_partial_failure_and_retry(repo, ready_item):
    broken = FakeChannel("broken")
    broken.fail_permanently = True
    working = FakeChannel("working")
    publisher = Publisher(repo, [broken, working])

    report = publisher.publish_item(ready_item.id)
    assert report.channels["broken"].outcome == "failed"
    assert report.channels["broken"].error
    assert report.channels["working"].outcome == "published"
    assert repo.get_item(ready_item.id).state == ItemState.FAILED
    assert len(working.posts) == 1

    broken.fail_permanently = False
    report = publisher.publish_item(ready_item.id)
    assert report.channels["broken"].outcome == "published"
    assert report.channels["working"].outcome == "skipped"
    assert len(broken.posts) == len(working.posts) == 1
    assert repo.get_item(ready_item.id).state == ItemState.PUBLISHED


def test_validation_failure_is_recorded(repo, ready_item):
    invalid = FakeChannel("invalid", max_len=1)
    valid = FakeChannel("valid")
    report = Publisher(repo, [invalid, valid]).publish_item(ready_item.id)
    assert report.channels["invalid"].outcome == "failed"
    assert not invalid.posts
    assert len(valid.posts) == 1
    assert repo.list_publications(ready_item.id)[0].status == PublicationStatus.FAILED


def test_transient_failure_retries(repo, ready_item):
    channel = FakeChannel("fake")
    channel.fail_times = 2
    pauses = []
    publisher = Publisher(repo, [channel], sleep=pauses.append)
    assert publisher.publish_item(ready_item.id).channels["fake"].outcome == "published"
    assert pauses == [1.0, 2.0]
    assert len(channel.posts) == 1
    assert repo.get_item(ready_item.id).state == ItemState.PUBLISHED


def test_unapproved_item_cannot_send(repo):
    item = repo.create_item("telegram")
    channel = FakeChannel("fake")
    with pytest.raises(IllegalTransition):
        Publisher(repo, [channel]).publish_item(item.id)
    assert not channel.posts
    assert repo.list_publications(item.id) == []


def test_crash_after_send_is_recovered(repo, ready_item):
    channel = FakeChannel("fake")
    channel.crash_after_send = True
    with pytest.raises(SimulatedCrash):
        Publisher(repo, [channel]).publish_item(ready_item.id)
    channel.crash_after_send = False
    assert repo.get_item(ready_item.id).state == ItemState.PUBLISHING
    assert (
        repo.list_publications(ready_item.id)[0].status == PublicationStatus.PUBLISHING
    )
    assert len(channel.posts) == 1

    report = Publisher(repo, [channel]).publish_item(ready_item.id)
    assert report.channels["fake"].outcome == "skipped"
    assert repo.get_item(ready_item.id).state == ItemState.PUBLISHING

    report = Publisher(repo, [channel], stale_after=timedelta(0)).publish_item(
        ready_item.id
    )
    assert report.channels["fake"].outcome == "published"
    assert len(channel.posts) == 1
    assert repo.list_publications(ready_item.id)[0].remote_id == next(
        iter(channel.posts)
    )
    assert repo.get_item(ready_item.id).state == ItemState.PUBLISHED


def test_old_claim_cannot_overwrite_recovered_publication(repo, ready_item):
    item_id = ready_item.id
    repo.transition(item_id, ItemState.PUBLISHING)
    old_claim = repo.claim_publication(item_id, "fake")
    recovered = repo.claim_publication(item_id, "fake", stale_after=timedelta(0))
    assert old_claim is not None and recovered is not None
    assert recovered.recovered
    assert recovered.stale

    with pytest.raises(LostPublicationClaim):
        repo.mark_failed(item_id, "fake", "Old worker failed", claim=old_claim)
    repo.mark_published(item_id, "fake", "existing-post", claim=recovered)
    assert repo.list_publications(item_id)[0].remote_id == "existing-post"
