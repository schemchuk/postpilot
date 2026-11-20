import os

import pytest
import test_store

from postpilot.core.states import IllegalTransition, ItemState
from postpilot.notify.approval import ApprovalHandler
from postpilot.notify.poller import poll_once

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
        self.updates = []
        self.offsets = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return len(self.sent)

    def answer_callback_query(self, id, text=None):
        pass

    def edit_message_text(self, chat_id, message_id, text):
        pass

    def get_updates(self, offset, timeout=0, *, limit=None):
        self.offsets.append(offset)
        return self.updates


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


def test_offset_persisted_after_batch_even_if_one_handler_fails(repo, caplog):
    api = FakeApi()
    state = repo.bot_state
    seen = []
    initial_offset = state.get_offset()
    item_id = awaiting(repo, "ua")

    def regenerate(item_id):
        seen.append((item_id, state.get_offset()))
        raise RuntimeError("private payload")

    handler = ApprovalHandler(repo, api, USER, on_regenerate=regenerate)
    next_item = awaiting(repo, "ua")
    api.updates = [
        {"update_id": 51, **callback("regen", item_id)},
        {"update_id": 52, **callback("approve", next_item)},
    ]
    poll_once(api, handler, state)
    assert api.offsets == [initial_offset, 53]
    assert seen == [(item_id, initial_offset)]
    assert repo.get_item(next_item).state == ItemState.APPROVED
    assert state.get_offset() == 53
    assert "RuntimeError" in caplog.text
    assert "private payload" not in caplog.text
