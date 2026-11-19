from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest

from postpilot.ai.client import AIError
from postpilot.ai.prompts import TRANSLATE_SYSTEM_PROMPT
from postpilot.core.limits import LINKEDIN_TEXT_LIMIT
from postpilot.core.models import Item, Variant
from postpilot.core.states import ItemState
from postpilot.notify.approval import ApprovalHandler
from postpilot.notify.telegram import Notifier
from postpilot.sources.telegram import TelegramInbox

USER, CHAT = 12, 34
ORIGINAL = "  Мій текст 🐧 #Linux\r\n\r\nВерсія 3: https://example.org  "
TRANSLATION = "Mein Text 🐧 #Linux\n\nVersion 3: https://example.org"


class FakeModel:
    def __init__(self, result=TRANSLATION):
        self.result = result
        self.calls = []

    def complete(self, system, user, schema=None):
        self.calls.append((system, user, schema))
        if isinstance(self.result, AIError):
            raise self.result
        return self.result


class FakeTelegramApi:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return len(self.sent)


@pytest.fixture
def repo():
    now = datetime.now(UTC)
    item = Item(uuid4(), "telegram", ItemState.AWAITING_APPROVAL, now, now, None, None)
    updates = {}

    def create(update_id, original, translation):
        updates[update_id] = item.id
        repository.get_variants.return_value = [
            Variant(item.id, "ua", original),
            Variant(item.id, "de", translation),
        ]
        return item

    repository = Mock()
    repository.has_inbox_update.side_effect = updates.__contains__
    repository.create_inbox_item.side_effect = create
    repository.apply_pending_edit.return_value = None
    return repository


def message(text, sender=USER):
    return {
        "update_id": 42,
        "message": {"from": {"id": sender}, "chat": {"id": CHAT}, "text": text},
    }


def test_allowed_text_is_translated_verbatim_and_previewed_once(repo):
    api, model = FakeTelegramApi(), FakeModel()
    handler = ApprovalHandler(repo, api, USER, inbox=TelegramInbox(repo, api, model))
    for _ in range(2):
        handler.handle_update(message(ORIGINAL))
    assert model.calls == [(TRANSLATE_SYSTEM_PROMPT, ORIGINAL, None)]
    repo.create_inbox_item.assert_called_once_with(42, ORIGINAL, TRANSLATION)
    [(chat, preview, markup)] = api.sent
    assert chat == CHAT
    assert ORIGINAL in preview and TRANSLATION in preview
    assert [button["text"] for button in markup["inline_keyboard"][0]] == [
        "Approve",
        "Edit",
        "Skip",
    ]


@pytest.mark.parametrize(
    "text,sender", [("/start", USER), ("  ", USER), (ORIGINAL, 999)]
)
def test_ignored_messages_do_not_reach_pending_edits_or_model(repo, text, sender):
    api, model = FakeTelegramApi(), FakeModel()
    ApprovalHandler(
        repo, api, USER, inbox=TelegramInbox(repo, api, model)
    ).handle_update(message(text, sender))
    assert not repo.mock_calls
    assert not model.calls
    assert not api.sent


@pytest.mark.parametrize(
    "original,result,reply",
    [
        ("я" * (LINKEDIN_TEXT_LIMIT + 1), TRANSLATION, "Original exceeds"),
        (ORIGINAL, "a" * (LINKEDIN_TEXT_LIMIT + 1), "Translation exceeds"),
        (ORIGINAL, AIError("private payload"), "Translation failed"),
        (ORIGINAL, "  ", "Translation failed"),
        (ORIGINAL, None, "Translation is not configured"),
    ],
)
def test_rejected_messages_create_no_item(repo, original, result, reply):
    api = FakeTelegramApi()
    model = FakeModel(result) if result is not None else None
    TelegramInbox(repo, api, model).handle_text(42, CHAT, original)
    repo.create_inbox_item.assert_not_called()
    [(chat, text, markup)] = api.sent
    assert chat == CHAT and text.startswith(reply) and markup is None
    assert "private payload" not in text
    if original != ORIGINAL:
        assert not model.calls


def test_pending_edit_takes_precedence(repo):
    api, model = FakeTelegramApi(), FakeModel()
    item = repo.create_inbox_item(41, ORIGINAL, TRANSLATION)
    repo.apply_pending_edit.return_value = item
    handler = ApprovalHandler(repo, api, USER, inbox=TelegramInbox(repo, api, model))
    handler.handle_update(message("Korrigierter Text"))
    repo.apply_pending_edit.assert_called_once_with(
        USER, "Korrigierter Text", update_id=42
    )
    repo.create_inbox_item.assert_called_once()
    assert model.calls == []
    assert len(api.sent) == 1


def test_long_preview_sends_both_complete_variants(repo):
    api = FakeTelegramApi()
    original, translation = "я" * LINKEDIN_TEXT_LIMIT, "a" * LINKEDIN_TEXT_LIMIT
    item = repo.create_inbox_item(42, original, translation)
    Notifier(api, CHAT).send_preview(item, repo.get_variants(item.id))
    [(_, ua, first_markup), (_, de, second_markup)] = api.sent
    assert ua.endswith(original) and de.endswith(translation)
    assert first_markup is None and second_markup is not None
