from unittest.mock import Mock
from uuid import uuid4

import pytest

from postpilot.notify.approval import ApprovalHandler


@pytest.mark.parametrize(
    ("action", "approve"), [("papprove", True), ("preject", False)]
)
def test_project_callback_checks_owner_chat_and_preview_revision(action, approve):
    owner = 11
    item_id = uuid4()
    store = Mock()
    store.resolve_preview.side_effect = [False, True, False]
    api = Mock()
    handler = ApprovalHandler(Mock(), api, owner, project_store=store)

    def receive(sender, chat, message_id):
        handler.handle_update(
            {
                "callback_query": {
                    "id": "callback",
                    "from": {"id": sender},
                    "data": f"{action}:{item_id}",
                    "message": {"chat": {"id": chat}, "message_id": message_id},
                }
            }
        )

    receive(owner + 1, owner, 10)
    receive(owner, owner + 1, 10)
    store.resolve_preview.assert_not_called()
    receive(owner, owner, 9)
    receive(owner, owner, 10)
    receive(owner, owner, 10)
    assert [call.args for call in store.resolve_preview.call_args_list] == [
        (item_id, message, owner, approve) for message in (9, 10, 10)
    ]
    assert [call.args[1] for call in api.send_message.call_args_list] == [
        "Already handled",
        "Project approved" if approve else "Project rejected",
        "Already handled",
    ]
    api.edit_message_reply_markup.assert_called_once_with(
        owner, 10, {"inline_keyboard": []}
    )
