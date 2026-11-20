from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest

from postpilot.core.models import Item
from postpilot.core.states import ItemState
from postpilot.notify.approval import ApprovalHandler
from postpilot.notify.telegram import TelegramError
from postpilot.stats import Stats, format_stats

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
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def record(self, operation, *args):
        self.calls.append((operation, *args))
        if self.fail_on == operation:
            raise TelegramError("sensitive reply details")

    def answer_callback_query(self, callback_id, text=None):
        self.record("answer", callback_id, text)

    def edit_message_text(self, chat_id, message_id, text):
        self.record("edit", chat_id, message_id, text)

    def edit_message_caption(self, chat_id, message_id, caption):
        self.record("caption", chat_id, message_id, caption)


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


@pytest.mark.parametrize("fail_on", [None, "answer", "edit"])
def test_approve_records_sender(fail_on, caplog):
    repo = MemoryRepo(ItemState.AWAITING_APPROVAL)
    api = RecordingApi(fail_on)
    ApprovalHandler(repo, api, USER).handle_update(callback(f"approve:{repo.item.id}"))
    assert repo.item.state == ItemState.APPROVED
    assert repo.item.approved_by == USER
    assert api.calls == [("answer", "button", None), ("edit", CHAT, 12, "Approved")]
    if fail_on:
        assert caplog.messages == ["Telegram callback reply failed"]


@pytest.mark.parametrize("fail_on", ["answer", "caption"])
def test_video_approval_persists_when_reply_fails(fail_on):
    class MemoryVideoStore:
        approved_by = None

        def approve(self, render_id, sender, approved_at):
            assert render_id == item_id
            self.approved_by = sender
            return True

    repo = MemoryRepo(ItemState.APPROVED)
    item_id = repo.item.id
    store = MemoryVideoStore()
    api = RecordingApi(fail_on)
    data = f"vapprove:{item_id}"
    update = callback(data)
    update["callback_query"]["message"].update(
        video={"file_id": "preview"},
        reply_markup={"inline_keyboard": [[{"callback_data": data}]]},
    )

    ApprovalHandler(repo, api, USER, video_store=store).handle_update(update)

    assert store.approved_by == USER
    assert api.calls == [
        ("answer", "button", None),
        ("caption", CHAT, 12, "Video approved"),
    ]


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


@pytest.mark.parametrize(
    ("text", "sender", "enabled", "inbox_enabled", "fails", "replies"),
    [
        ("/stats", USER, True, True, False, True),
        ("/stats@my_bot ignored args", USER, True, False, False, True),
        ("/stats ignored", USER, True, True, True, True),
        ("/stats", 999, True, True, False, False),
        ("/stats", USER, False, False, False, False),
        ("/stats_extra", USER, True, True, False, False),
        ("/unknown", USER, True, True, False, False),
    ],
)
def test_stats_command_routing(text, sender, enabled, inbox_enabled, fails, replies):
    repo, inbox, api, stats = Mock(), Mock(), Mock(), Mock()
    result = Stats()
    stats.read.return_value = result
    if fails:
        api.send_message.side_effect = TelegramError("sensitive reply details")
    handler = ApprovalHandler(
        repo,
        api,
        USER,
        inbox=inbox if inbox_enabled else None,
        stats=stats if enabled else None,
    )
    handler.handle_update(
        {
            "update_id": 17,
            "message": {"from": {"id": sender}, "chat": {"id": CHAT}, "text": text},
        }
    )
    assert repo.mock_calls == inbox.mock_calls == []
    if replies:
        stats.read.assert_called_once()
        assert stats.read.call_args.args[0].tzinfo is UTC
        api.send_message.assert_called_once_with(
            CHAT, format_stats(result), parse_mode="HTML"
        )
    else:
        assert stats.mock_calls == api.mock_calls == []
