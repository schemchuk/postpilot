from unittest.mock import Mock

import httpx
import pytest
import test_store

from postpilot.channels.linkedin import LinkedInChannel
from postpilot.channels.threads import ThreadsChannel
from postpilot.config import LinkedInSettings, Settings, ThreadsSettings
from postpilot.core.models import PublicationStatus
from postpilot.core.publisher import Publisher
from postpilot.core.states import ItemState
from postpilot.jobs import tasks

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn
repo = test_store.repo


class RecordingChannel:
    def __init__(self, name):
        self.name = name
        self.validated = []
        self.sent = []

    def validate(self, item, variants):
        self.validated.append(item.id)

    def publish(self, item, variants):
        self.sent.append(item.id)
        return f"remote-{self.name}"

    def find_existing(self, item_id):
        return None


def test_held_channel_is_released_on_rerun(repo):
    item = repo.create_item("telegram")
    repo.transition(item.id, ItemState.AWAITING_APPROVAL)
    repo.transition(item.id, ItemState.APPROVED, approved_by=123)
    expired = RecordingChannel("expired")
    healthy = RecordingChannel("healthy")
    usable = {healthy.name}
    publisher = Publisher(repo, [expired, healthy], token_guard=usable.__contains__)

    assert repo.list_unfinished_item_ids() == []
    report = publisher.publish_item(item.id)
    assert report.channels[expired.name].outcome == "held"
    assert report.channels[healthy.name].outcome == "published"
    assert expired.validated == expired.sent == []
    assert healthy.sent == [item.id]
    assert repo.get_item(item.id).state == ItemState.PUBLISHING
    assert repo.list_unfinished_item_ids() == [item.id]
    assert [(row.channel, row.status) for row in repo.list_publications(item.id)] == [
        (healthy.name, PublicationStatus.PUBLISHED)
    ]

    usable.add(expired.name)
    report = publisher.publish_item(item.id)
    assert report.channels[expired.name].outcome == "published"
    assert report.channels[healthy.name].outcome == "skipped"
    assert expired.sent == healthy.sent == [item.id]
    assert repo.get_item(item.id).state == ItemState.PUBLISHED
    assert repo.list_unfinished_item_ids() == []


def test_finish_publishing_waits_for_missing_publication(repo):
    item = repo.create_item("telegram")
    repo.transition(item.id, ItemState.AWAITING_APPROVAL)
    repo.transition(item.id, ItemState.APPROVED, approved_by=123)
    repo.transition(item.id, ItemState.PUBLISHING)
    repo.finish_publishing(item.id, {"held"})
    assert repo.get_item(item.id).state == ItemState.PUBLISHING
    assert repo.list_publications(item.id) == []


@pytest.mark.parametrize("source", ["github", "telegram"])
@pytest.mark.parametrize("legacy", [None, ItemState.PUBLISHING, ItemState.FAILED])
def test_active_text_targets_finish_with_legacy_ua_row(
    repo, monkeypatch, source, legacy
):
    item = repo.create_item(source)
    repo.add_variant(item.id, "ua", "Українська")
    repo.add_variant(item.id, "de", "Deutsch")
    repo.transition(item.id, ItemState.AWAITING_APPROVAL)
    repo.transition(item.id, ItemState.APPROVED, approved_by=123)
    previous = None
    if legacy is not None:
        repo.transition(item.id, ItemState.PUBLISHING)
        claim = repo.claim_publication(item.id, "linkedin-ua")
        if legacy == ItemState.FAILED:
            repo.mark_failed(item.id, "linkedin-ua", "legacy failure", claim=claim)
            repo.transition(item.id, ItemState.FAILED)
        (previous,) = repo.list_publications(item.id)
        assert item.id in repo.list_unfinished_item_ids(source=source)

    settings = Settings(
        "db",
        "bot",
        1,
        "key",
        linkedin=LinkedInSettings("id", "secret", "uri", ()),
        threads=ThreadsSettings("id", "secret", "uri"),
    )
    sent = []

    def publish(channel, item, variants, **kwargs):
        sent.append(channel.name)
        return f"remote-{channel.name}"

    monkeypatch.setattr(tasks, "_read_token", Mock(return_value="test-token"))
    for adapter in (LinkedInChannel, ThreadsChannel):
        monkeypatch.setattr(adapter, "validate", Mock())
    monkeypatch.setattr(LinkedInChannel, "publish", publish)
    monkeypatch.setattr(ThreadsChannel, "publish_with_ref", publish)

    with httpx.Client() as client:
        publisher = tasks._publisher(settings, repo, client)
        report = publisher.publish_item(item.id)

    assert sent == ["linkedin", "threads"]
    assert {name: result.outcome for name, result in report.channels.items()} == {
        "linkedin": "published",
        "threads": "published",
    }
    assert repo.get_item(item.id).state == ItemState.PUBLISHED
    assert item.id not in repo.list_unfinished_item_ids()
    publications = {row.channel: row for row in repo.list_publications(item.id)}
    assert publications.pop("linkedin-ua", None) == previous
    assert {name: row.status for name, row in publications.items()} == {
        "linkedin": PublicationStatus.PUBLISHED,
        "threads": PublicationStatus.PUBLISHED,
    }
