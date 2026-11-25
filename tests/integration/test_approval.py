import os
from unittest.mock import Mock, call

import pytest
import test_store
from test_publisher_hold import RecordingChannel

from postpilot.core.models import PublicationStatus
from postpilot.core.publisher import Publisher
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.notify.approval import ApprovalHandler

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is unset"
    ),
]

USER = 12345678901
CHAT = 39


database_schema = test_store.database_schema
conn = test_store.conn
repo = test_store.repo


class FakeApi:
    def __init__(self):
        self.sent = []

    def send_message(
        self, chat_id, text, reply_markup=None, *, reply_to_message_id=None
    ):
        self.sent.append((chat_id, text, reply_markup))
        return len(self.sent)

    def answer_callback_query(self, id, text=None):
        pass

    def edit_message_text(self, chat_id, message_id, text):
        pass


def awaiting(repo, *languages):
    item = repo.create_item("github")
    for language in languages:
        repo.add_variant(item.id, language, "Original")
    return repo.transition(item.id, ItemState.AWAITING_APPROVAL).id


def callback(action, item_id):
    return {
        "callback_query": {
            "from": {"id": USER},
            "id": "button",
            "data": f"{action}:{item_id}",
            "message": {"chat": {"id": CHAT}, "message_id": 11},
        }
    }


def message(text):
    return {"message": {"from": {"id": USER}, "chat": {"id": CHAT}, "text": text}}


def test_approve_skip_and_regenerate(repo):
    regenerated = []
    handler = ApprovalHandler(repo, FakeApi(), USER, on_regenerate=regenerated.append)
    approved_id = awaiting(repo, "ua")
    handler.handle_update(callback("approve", approved_id))
    approved = repo.get_item(approved_id)
    assert approved.state == ItemState.APPROVED
    assert approved.approved_by == USER
    assert approved.approved_at == approved.updated_at

    skipped_id = awaiting(repo, "ua")
    handler.handle_update(callback("skip", skipped_id))
    assert repo.get_item(skipped_id).state == ItemState.SKIPPED

    regen_id = awaiting(repo, "ua")
    handler.handle_update(callback("regen", regen_id))
    handler.handle_update(callback("regen", regen_id))
    assert repo.get_item(regen_id).state == ItemState.DRAFT
    assert regenerated == [regen_id]


@pytest.mark.parametrize("languages", [("de", "ua"), ("ua",)])
def test_edit_consumes_pending_request_and_resends_preview(repo, languages):
    api = FakeApi()
    handler = ApprovalHandler(repo, api, USER)
    item_id = awaiting(repo, *languages)
    handler.handle_update(message("Ignored"))
    assert api.sent == []
    handler.handle_update(callback("edit", item_id))
    assert api.sent == [(CHAT, f"Send the new {languages[0].upper()} text.", None)]
    handler.handle_update(message("New <text>"))
    handler.handle_update(message("Ignored again"))
    variants = repo.get_variants(item_id)
    assert [variant.text for variant in variants] == ["New <text>"] + ["Original"] * (
        len(languages) - 1
    )
    assert len(api.sent) == 2
    assert "New <text>" in api.sent[-1][1]
    assert api.sent[-1][2]["inline_keyboard"]


def test_edit_refused_after_approval(repo):
    api = FakeApi()
    handler = ApprovalHandler(repo, api, USER)
    item_id = awaiting(repo, "ua")
    handler.handle_update(callback("edit", item_id))
    handler.handle_update(callback("approve", item_id))
    handler.handle_update(message("Too late"))
    with pytest.raises(IllegalTransition):
        repo.update_variant_text(item_id, "ua", "Still too late")
    assert repo.bot_state.take_pending_edit(USER) is None
    assert repo.get_variants(item_id)[0].text == "Original"
    assert len(api.sent) == 1


@pytest.mark.parametrize("action", ["retry", "skipfail"])
@pytest.mark.parametrize("source", ["github", "own_video"])
def test_resolve_failed_item(repo, action, source):
    item = repo.create_item(source)
    repo.transition(item.id, ItemState.AWAITING_APPROVAL)
    repo.transition(item.id, ItemState.APPROVED, approved_by=USER)
    repo.transition(item.id, ItemState.PUBLISHING)
    repo.claim_publication(item.id, "linkedin")
    repo.mark_published(item.id, "linkedin", "already-published")
    failed_channel = "facebook-reels" if source == "own_video" else "threads"
    repo.claim_publication(item.id, failed_channel)
    repo.mark_failed(item.id, failed_channel, "failed")
    repo.transition(item.id, ItemState.FAILED)

    linkedin = RecordingChannel("linkedin")
    threads = RecordingChannel("threads")
    reels = RecordingChannel("facebook-reels")
    publisher = Publisher(
        repo,
        [linkedin, threads],
        own_video_channels=[linkedin, threads],
        own_video_held_channels=(reels.name,),
    )
    api = Mock()
    handler = ApprovalHandler(repo, api, USER, on_retry=publisher.publish_item)
    update = callback(action, item.id)
    handler.handle_update(update)
    handler.handle_update(update)
    api.answer_callback_query.assert_not_called()
    assert api.send_message.call_args_list == [
        call(
            CHAT, "Retrying" if action == "retry" else "Skipped", reply_to_message_id=11
        ),
        call(CHAT, "Already handled", reply_to_message_id=11),
    ]
    api.edit_message_reply_markup.assert_called_once_with(
        CHAT, 11, {"inline_keyboard": []}
    )
    assert linkedin.sent == reels.sent == []
    publications = {row.channel: row for row in repo.list_publications(item.id)}
    assert publications["linkedin"].remote_id == "already-published"

    if action == "skipfail":
        assert threads.sent == []
        assert repo.get_item(item.id).state == ItemState.SKIPPED
    elif source == "own_video":
        assert threads.sent == [item.id]
        assert repo.get_item(item.id).state == ItemState.PUBLISHING
        assert publications[reels.name].status == PublicationStatus.FAILED
        reels_publisher = Publisher(
            repo,
            [],
            own_video_channels=[reels],
            own_video_held_channels=(linkedin.name, threads.name),
        )
        reels_publisher.publish_item(item.id)
        assert reels.sent == [item.id]
        assert repo.get_item(item.id).state == ItemState.PUBLISHED
    else:
        assert threads.sent == [item.id]
        assert publications["threads"].status == PublicationStatus.PUBLISHED
        assert repo.get_item(item.id).state == ItemState.PUBLISHED


def test_old_failure_button_cannot_act_on_a_later_failure(repo):
    item_id = awaiting(repo, "de")
    repo.transition(item_id, ItemState.APPROVED, approved_by=USER)
    repo.transition(item_id, ItemState.PUBLISHING)
    repo.transition(item_id, ItemState.FAILED)

    def fail_again(item_id):
        repo.transition(item_id, ItemState.FAILED)

    api = Mock()
    retry = Mock(side_effect=fail_again)
    handler = ApprovalHandler(repo, api, USER, on_retry=retry)
    handler.handle_update(callback("retry", item_id))
    handler.handle_update(callback("skipfail", item_id))
    assert repo.get_item(item_id).state == ItemState.FAILED
    api.answer_callback_query.assert_not_called()
    api.send_message.assert_called_with(CHAT, "Already handled", reply_to_message_id=11)
    retry.assert_called_once_with(item_id)

    new_message = callback("skipfail", item_id)
    new_message["callback_query"]["message"]["message_id"] = 12
    handler.handle_update(new_message)
    assert repo.get_item(item_id).state == ItemState.SKIPPED
