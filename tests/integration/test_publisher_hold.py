import test_store

from postpilot.core.models import PublicationStatus
from postpilot.core.publisher import Publisher
from postpilot.core.states import ItemState

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
