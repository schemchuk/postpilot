import json
import logging
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from postpilot.core.models import Item, Variant
from postpilot.core.states import ItemState
from postpilot.notify.telegram import (
    Notifier,
    TelegramApi,
    TelegramError,
    approval_keyboard,
    render_preview,
)

TOKEN = "123:private-test-token"
URL = f"https://api.telegram.org/bot{TOKEN}"


def test_request_shapes(caplog):
    calls = []

    def respond(request):
        assert request.method == "POST"
        method = str(request.url).removeprefix(URL + "/")
        calls.append((method, json.loads(request.content)))
        results = {"sendMessage": {"message_id": 42}, "getUpdates": [{"update_id": 8}]}
        return httpx.Response(
            200, json={"ok": True, "result": results.get(method, True)}
        )

    caplog.set_level(logging.INFO, logger="httpx")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        api = TelegramApi(TOKEN, client=client)
        markup = approval_keyboard(uuid4())
        assert api.send_message(12, "<b>plain</b>", markup) == 42
        Notifier(api, 12).send_alert("x" * 4200)
        api.edit_message_text(12, 42, "Approved")
        api.answer_callback_query("callback")
        api.answer_callback_query("old", "Already handled")
        assert api.get_updates(8) == [{"update_id": 8}]
    assert calls == [
        (
            "sendMessage",
            {"chat_id": 12, "text": "<b>plain</b>", "reply_markup": markup},
        ),
        ("sendMessage", {"chat_id": 12, "text": "x" * 4095 + "…"}),
        ("editMessageText", {"chat_id": 12, "message_id": 42, "text": "Approved"}),
        ("answerCallbackQuery", {"callback_query_id": "callback"}),
        (
            "answerCallbackQuery",
            {"callback_query_id": "old", "text": "Already handled"},
        ),
        ("getUpdates", {"offset": 8, "timeout": 0}),
    ]
    assert TOKEN not in caplog.text
    assert TOKEN not in repr(api)


@pytest.mark.parametrize("failure", ["api", "status", "transport"])
def test_failures_are_redacted(failure):
    def respond(request):
        if failure == "transport":
            raise httpx.ConnectError(str(request.url), request=request)
        return httpx.Response(
            200 if failure == "api" else 403,
            json={"ok": False, "description": str(request.url)},
        )

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        pytest.raises(TelegramError) as caught,
    ):
        TelegramApi(TOKEN, client=client).get_updates(0)
    assert caught.value.__context__ is None
    for output in (str(caught.value), repr(caught.value)):
        assert TOKEN not in output
        assert URL not in output


def test_preview_and_keyboard():
    item_id = uuid4()
    now = datetime.now(UTC)
    item = Item(item_id, "github", ItemState.AWAITING_APPROVAL, now, now, None, None)
    preview = render_preview(
        item, [Variant(item_id, "de", "Hallo"), Variant(item_id, "ua", "О" * 4096)]
    )
    assert preview.startswith(f"Item {item_id.hex[:8]} · github\n\nDE\nHallo\n\nUA\n")
    assert len(preview) == 4096
    assert preview.endswith("…")
    buttons = approval_keyboard(item_id)["inline_keyboard"][0]
    assert [button["text"] for button in buttons] == [
        "Approve",
        "Edit",
        "Regenerate",
        "Skip",
    ]
    assert [button["callback_data"] for button in buttons] == [
        f"{action}:{item_id}" for action in ("approve", "edit", "regen", "skip")
    ]
    assert all(len(button["callback_data"].encode()) < 64 for button in buttons)


def test_long_first_variant_leaves_room_for_second_language():
    item_id = uuid4()
    now = datetime.now(UTC)
    item = Item(item_id, "github", ItemState.AWAITING_APPROVAL, now, now, None, None)
    preview = render_preview(
        item, [Variant(item_id, "de", "x" * 4096), Variant(item_id, "ua", "Привіт")]
    )
    assert len(preview) == 4096
    assert preview.endswith("…\n\nUA\nПривіт")
