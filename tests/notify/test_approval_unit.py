from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from postpilot.core.models import Item
from postpilot.core.states import ItemState
from postpilot.notify.approval import ApprovalHandler

USER = 321
CHAT = 123


class MemoryRepo:
    def __init__(self, state):
        now = datetime.now(UTC)
        self.item = Item(uuid4(), "telegram", state, now, now, None, None)
        self.calls = []

    def get_item(self, item_id):
        self.calls.append(("get_item", item_id))
        return self.item

    def transition(self, item_id, state, *, expected_state, approved_by):
        self.calls.append(("transition", item_id, state, expected_state, approved_by))
        self.item = replace(self.item, state=state, approved_by=approved_by)

    def apply_pending_edit(self, sender, text):
        self.calls.append(("apply_pending_edit", sender, text))


class RecordingApi:
    def __init__(self):
        self.calls = []

    def answer_callback_query(self, callback_id, text=None):
        self.calls.append(("answer", callback_id, text))

    def edit_message_text(self, chat_id, message_id, text):
        self.calls.append(("edit", chat_id, message_id, text))


def callback(data, sender=USER):
    return {
        "callback_query": {
            "from": {"id": sender},
            "id": "button",
            "data": data,
            "message": {"chat": {"id": CHAT}, "message_id": 12},
        }
    }


def test_foreign_sender_is_ignored_before_any_calls():
    repo = MemoryRepo(ItemState.AWAITING_APPROVAL)
    api = RecordingApi()
    handler = ApprovalHandler(repo, api, USER)
    handler.handle_update(callback(f"approve:{repo.item.id}", sender=999))
    handler.handle_update(
        {"message": {"from": {"id": 999}, "chat": {"id": CHAT}, "text": "x"}}
    )
    assert repo.calls == api.calls == []


def test_approve_records_sender():
    repo = MemoryRepo(ItemState.AWAITING_APPROVAL)
    api = RecordingApi()
    ApprovalHandler(repo, api, USER).handle_update(callback(f"approve:{repo.item.id}"))
    assert repo.item.state == ItemState.APPROVED
    assert repo.item.approved_by == USER
    assert api.calls == [("answer", "button", None), ("edit", CHAT, 12, "Approved")]


@pytest.mark.parametrize("state", set(ItemState) - {ItemState.AWAITING_APPROVAL})
@pytest.mark.parametrize("action", ["approve", "edit", "skip", "regen"])
def test_stale_button_only_answers(state, action):
    repo = MemoryRepo(state)
    api = RecordingApi()
    ApprovalHandler(repo, api, USER).handle_update(callback(f"{action}:{repo.item.id}"))
    assert repo.calls == [("get_item", repo.item.id)]
    assert api.calls == [("answer", "button", "Already handled")]


@pytest.mark.parametrize(
    "data", ["approve", "edit:bad-id", "skip:a:b", "unknown", None, 42]
)
def test_malformed_callback_is_ignored(data):
    repo = MemoryRepo(ItemState.AWAITING_APPROVAL)
    api = RecordingApi()
    ApprovalHandler(repo, api, USER).handle_update(callback(data))
    assert repo.calls == api.calls == []
