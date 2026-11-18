import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from postpilot.notify.approval import ApprovalHandler
from postpilot.notify.renewal import RenewalHandler
from postpilot.notify.telegram import Notifier, TelegramApi
from postpilot.store.repo import ItemRepo
from postpilot.tokens.base import TokenManager
from postpilot.tokens.oauth_state import make_state
from postpilot.tokens.service import issue_renewal_prompt

KEY = b"test key"
NOW = datetime(2026, 11, 18, tzinfo=UTC)
CHAT = 45
ALLOWED = 42
FAILURE = "Token renewal failed. Use a fresh renewal link."


class MemoryState:
    def __init__(self):
        self.nonces = set()

    def save_oauth_nonce(self, nonce):
        self.nonces.add(nonce)

    def consume_oauth_nonce(self, nonce):
        if nonce not in self.nonces:
            return False
        self.nonces.remove(nonce)
        return True


@pytest.fixture
def setup():
    store = MemoryState()
    manager = Mock(spec=TokenManager)
    manager.provider = "linkedin"
    manager.complete_renewal.return_value = NOW + timedelta(days=60)
    messages = []

    def request(req):
        method = req.url.path.rsplit("/", 1)[1]
        messages.append((method, json.loads(req.content)))
        if method == "deleteMessage":
            return httpx.Response(403, json={"ok": False})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 101}})

    with httpx.Client(transport=httpx.MockTransport(request)) as client:
        api = TelegramApi("test", client=client)
        resumed = Mock()
        renewal = RenewalHandler(
            {"linkedin": manager},
            KEY,
            api,
            resumed,
            state_store=store,
            clock=lambda: NOW,
        )
        repo = Mock(spec=ItemRepo)
        approval = ApprovalHandler(repo, api, ALLOWED, renewal=renewal)

        def send(text, sender=ALLOWED):
            approval.handle_update(
                {
                    "message": {
                        "from": {"id": sender},
                        "chat": {"id": CHAT},
                        "message_id": 17,
                        "text": text,
                    },
                }
            )
            repo.apply_pending_edit.assert_not_called()

        yield store, manager, api, messages, resumed, send


def test_prompt_command_and_replay(setup):
    store, manager, api, messages, resumed, send = setup
    manager.renewal_url.side_effect = lambda state: (
        f"https://example.test/?state={state}"
    )
    issue_renewal_prompt(manager, KEY, store, Notifier(api, CHAT), NOW)
    _, payload = messages.pop()
    button = payload["reply_markup"]["inline_keyboard"][0][0]
    assert button["text"] == "Renew LinkedIn token"
    (state,) = parse_qs(urlsplit(button["url"]).query)["state"]
    command = f"/renew example-code {state}"

    send(command, sender=2)
    assert messages == []
    manager.complete_renewal.assert_not_called()

    send(command)
    assert messages == [
        ("deleteMessage", {"chat_id": CHAT, "message_id": 17}),
        (
            "sendMessage",
            {"chat_id": CHAT, "text": "LinkedIn token renewed until 2027-01-17"},
        ),
    ]
    manager.complete_renewal.assert_called_once_with("example-code")
    resumed.assert_called_once_with("linkedin")
    send(command)
    assert messages[-1][1]["text"] == FAILURE
    manager.complete_renewal.assert_called_once()
    resumed.assert_called_once()


def test_invalid_expired_and_malformed_commands(setup):
    store, manager, _, messages, resumed, send = setup
    send("/renew example-code invalid")
    assert messages[-1][1]["text"] == FAILURE
    expired = make_state(KEY, "linkedin", NOW - timedelta(minutes=31), store=store)
    send(f"/renew example-code {expired}")
    assert messages[-1][1]["text"] == FAILURE
    send(f"/renew example-code {expired} extra")
    assert (
        messages[-1][1]["text"] == "Use /renew <code> <state> from the callback page."
    )
    send("/renew")
    assert (
        messages[-1][1]["text"] == "Use /renew <code> <state> from the callback page."
    )
    manager.complete_renewal.assert_not_called()
    resumed.assert_not_called()


def test_exchange_failure_has_generic_reply(setup):
    store, manager, _, messages, resumed, send = setup
    state = make_state(KEY, "linkedin", NOW, store=store)
    manager.complete_renewal.side_effect = RuntimeError("sensitive detail")
    send(f"/renew example-code {state}")
    assert messages[-1][1]["text"] == FAILURE
    manager.complete_renewal.assert_called_once()
    resumed.assert_not_called()
