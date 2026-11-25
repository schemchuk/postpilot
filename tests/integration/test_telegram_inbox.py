from unittest.mock import Mock

import pytest
import test_store

from postpilot.core.limits import LINKEDIN_TEXT_LIMIT
from postpilot.core.states import ItemState
from postpilot.notify.approval import ApprovalHandler
from postpilot.notify.telegram import TelegramError
from postpilot.sources.telegram import TelegramInbox

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn
repo = test_store.repo


def test_inbox_creates_both_variants_and_deduplicates_after_preview_failure(repo, conn):
    model = Mock()
    model.complete.return_value = "Mein Text"
    api = Mock()
    api.send_message.side_effect = TelegramError("send failed")
    original = "  Мій текст\r\n\r\nРядок 2 🐧  "
    inbox = TelegramInbox(repo, api, model)
    with pytest.raises(TelegramError):
        inbox.handle_text(101, 12, original)
    inbox.handle_text(101, 12, original)
    api.send_message.assert_called_once()
    model.complete.assert_called_once()
    with conn.transaction():
        [(item_id,)] = conn.execute(
            "SELECT item_id FROM inbox_updates WHERE update_id = 101"
        ).fetchall()
        assert conn.execute(
            "SELECT count(*) FROM items WHERE id = %s", (item_id,)
        ).fetchone() == (1,)
    item = repo.get_item(item_id)
    assert (item.source, item.state) == ("telegram", ItemState.AWAITING_APPROVAL)
    assert {variant.lang: variant.text for variant in repo.get_variants(item_id)} == {
        "ua": original,
        "de": "Mein Text",
    }


def test_inbox_transaction_rolls_back_if_second_variant_fails(repo, conn, monkeypatch):
    add_variant = repo.add_variant

    def fail_second(item_id, lang, text):
        if lang == "de":
            raise RuntimeError("second variant failed")
        add_variant(item_id, lang, text)

    def counts():
        with conn.transaction():
            return conn.execute(
                "SELECT (SELECT count(*) FROM items), (SELECT count(*) FROM variants)"
            ).fetchone()

    before = counts()
    with monkeypatch.context() as patch:
        patch.setattr(repo, "add_variant", fail_second)
        with pytest.raises(RuntimeError, match="second variant failed"):
            repo.create_inbox_item(102, "Оригінал", "Original")
    assert counts() == before
    assert not repo.has_inbox_update(102)
    assert repo.create_inbox_item(102, "Оригінал", "Original") is not None
    assert repo.create_inbox_item(102, "Інший", "Anders") is None
    assert counts() == (before[0] + 1, before[1] + 2)


def test_inbox_edit_updates_only_german_and_can_retry_oversize_edit(repo):
    original = "  Оригінал  "
    item = repo.create_inbox_item(103, original, "Original")
    api = Mock()
    model = Mock()
    handler = ApprovalHandler(repo, api, 12, inbox=TelegramInbox(repo, api, model))
    callback = {
        "from": {"id": 12},
        "id": "button",
        "message": {"chat": {"id": 12}, "message_id": 11},
    }
    handler.handle_update({"callback_query": {**callback, "data": f"regen:{item.id}"}})
    assert repo.get_item(item.id).state == ItemState.AWAITING_APPROVAL
    api.send_message.assert_called_once_with(
        12, "Regeneration is unavailable", reply_to_message_id=11
    )
    api.send_message.reset_mock()
    handler.handle_update({"callback_query": {**callback, "data": f"edit:{item.id}"}})
    api.send_message.assert_called_once_with(12, "Send the new DE text.")
    api.answer_callback_query.assert_not_called()
    message = {"from": {"id": 12}, "chat": {"id": 12}}
    handler.handle_update(
        {
            "update_id": 104,
            "message": {**message, "text": "a" * (LINKEDIN_TEXT_LIMIT + 1)},
        }
    )
    assert "exceeds" in api.send_message.call_args.args[1]
    update = {"update_id": 105, "message": {**message, "text": "Geändert"}}
    handler.handle_update(update)
    handler.handle_update(update)
    assert {variant.lang: variant.text for variant in repo.get_variants(item.id)} == {
        "ua": original,
        "de": "Geändert",
    }
    assert api.send_message.call_count == 3
    model.complete.assert_not_called()
