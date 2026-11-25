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
        api.send_message(12, "Already handled", reply_to_message_id=42)
        Notifier(api, 12).send_alert("x" * 4200)
        api.edit_message_text(12, 42, "Approved")
        api.answer_callback_query("callback")
        api.answer_callback_query("old", "Already handled")
        assert api.get_updates(8) == [{"update_id": 8}]
        assert api.get_updates(8, limit=1) == [{"update_id": 8}]
    assert calls == [
        (
            "sendMessage",
            {"chat_id": 12, "text": "<b>plain</b>", "reply_markup": markup},
        ),
        (
            "sendMessage",
            {
                "chat_id": 12,
                "text": "Already handled",
                "reply_parameters": {
                    "message_id": 42,
                    "allow_sending_without_reply": True,
                },
            },
        ),
        ("sendMessage", {"chat_id": 12, "text": "x" * 4095 + "…"}),
        ("editMessageText", {"chat_id": 12, "message_id": 42, "text": "Approved"}),
        ("answerCallbackQuery", {"callback_query_id": "callback"}),
        (
            "answerCallbackQuery",
            {"callback_query_id": "old", "text": "Already handled"},
        ),
        ("getUpdates", {"offset": 8, "timeout": 0}),
        ("getUpdates", {"offset": 8, "timeout": 0, "limit": 1}),
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
    assert preview.startswith(
        f"Item {item_id.hex[:8]} · github\n"
        "Channels: LinkedIn DE, Threads\n\nDE\nHallo\n\n"
        "UA (not published: LinkedIn UA paused)\n"
    )
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
    assert preview.endswith("…\n\nUA (not published: LinkedIn UA paused)\nПривіт")


@pytest.mark.parametrize("caption", [None, "Profile advice"])
def test_send_document_multipart(caption):
    content = "## Поради".encode()

    def respond(request):
        assert request.method == "POST"
        assert str(request.url) == f"{URL}/sendDocument"
        assert request.headers["content-type"].startswith(
            "multipart/form-data; boundary="
        )
        body = request.read()
        assert b'name="chat_id"\r\n\r\n42\r\n' in body
        assert b'name="document"; filename="profile-advice-2025-01-01.md"' in body
        assert b"Content-Type: text/markdown\r\n\r\n" + content in body
        if caption:
            assert b'name="caption"\r\n\r\nProfile advice\r\n' in body
        else:
            assert b'name="caption"' not in body
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 12}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        TelegramApi(TOKEN, client=client).send_document(
            42, "profile-advice-2025-01-01.md", content, caption
        )


def test_send_document_error_excludes_token():
    def respond(request):
        raise httpx.ConnectError(str(request.url), request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        pytest.raises(TelegramError, match="^Telegram sendDocument failed$") as error,
    ):
        TelegramApi(TOKEN, client=client).send_document(42, "advice.md", b"advice")
    assert error.value.__context__ is None


def test_get_file_download_and_send_video_reference(caplog):
    def respond(request):
        if request.url.path.endswith("/getFile"):
            assert json.loads(request.content) == {"file_id": "document"}
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "files/answer.txt"}}
            )
        if request.method == "GET":
            assert request.url.path == f"/file/bot{TOKEN}/files/answer.txt"
            return httpx.Response(200, content="Відповідь".encode())
        assert request.url.path.endswith("/sendVideo")
        assert json.loads(request.content) == {
            "chat_id": 12,
            "caption": "",
            "video": "file",
        }
        return httpx.Response(200, json={"ok": True, "result": True})

    caplog.set_level(logging.INFO, logger="httpx")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        api = TelegramApi(TOKEN, client=client)
        assert api.download_file(api.get_file("document")).decode() == "Відповідь"
        api.send_video(12, "file")
    assert TOKEN not in caplog.text
