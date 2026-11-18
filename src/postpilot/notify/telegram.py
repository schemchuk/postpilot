import logging
from collections.abc import Iterable
from typing import Any, cast
from uuid import UUID

import httpx

from postpilot.core.models import Item, Variant

_MESSAGE_LIMIT = 4096
_BOT_URL = "https://api.telegram.org/bot"


class TelegramError(RuntimeError):
    pass


def _exclude_bot_requests(record: logging.LogRecord) -> bool:
    # HTTPX includes the full credential-bearing URL in its request log.
    return _BOT_URL not in record.getMessage()


logging.getLogger("httpx").addFilter(_exclude_bot_requests)


class TelegramApi:
    def __init__(self, token: str, *, client: httpx.Client | None = None) -> None:
        self._token = token
        self._client = client

    def _request(self, method: str, **payload: object) -> Any:
        url = f"{_BOT_URL}{self._token}/{method}"
        try:
            if self._client is None:
                response = httpx.post(url, json=payload, follow_redirects=False)
            else:
                response = self._client.post(url, json=payload, follow_redirects=False)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, ValueError):
            pass
        else:
            if isinstance(data, dict) and data.get("ok") is True and "result" in data:
                return data["result"]
        # Raise outside the exception handler to avoid retaining the request exception.
        raise TelegramError(f"Telegram {method} failed")

    def send_message(
        self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None
    ) -> int:
        payload: dict[str, object] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = self._request("sendMessage", **payload)
        if (
            isinstance(result, dict)
            and type(message_id := result.get("message_id")) is int
        ):
            return message_id
        raise TelegramError("Telegram sendMessage returned an invalid message id")

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        self._request(
            "editMessageText", chat_id=chat_id, message_id=message_id, text=text
        )

    def answer_callback_query(self, id: str, text: str | None = None) -> None:
        payload = {} if text is None else {"text": text}
        self._request("answerCallbackQuery", callback_query_id=id, **payload)

    def get_updates(self, offset: int, timeout: int = 0) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            self._request("getUpdates", offset=offset, timeout=timeout),
        )


def _truncate(text: str, limit: int = _MESSAGE_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_preview(item: Item, variants: Iterable[Variant]) -> str:
    preview = f"Item {item.id.hex[:8]} · {item.source}"
    entries = [(f"\n\n{variant.lang.upper()}\n", variant.text) for variant in variants]
    remaining = (
        _MESSAGE_LIMIT - len(preview) - sum(len(header) for header, _ in entries)
    )
    for index, (header, text) in enumerate(entries):
        # Reserve room for later variants, even if the first text is very long.
        budget = remaining - sum(
            min(len(later), remaining // (len(entries) - index))
            for _, later in entries[index + 1 :]
        )
        excerpt = _truncate(text, budget)
        preview += header + excerpt
        remaining -= len(excerpt)
    return preview


def approval_keyboard(item_id: UUID) -> dict[str, list[list[dict[str, str]]]]:
    return {
        "inline_keyboard": [
            [
                {"text": label, "callback_data": f"{action}:{item_id}"}
                for label, action in (
                    ("Approve", "approve"),
                    ("Edit", "edit"),
                    ("Regenerate", "regen"),
                    ("Skip", "skip"),
                )
            ]
        ]
    }


class Notifier:
    def __init__(self, api: TelegramApi, chat_id: int) -> None:
        self.api = api
        self.chat_id = chat_id

    def send_preview(self, item: Item, variants: Iterable[Variant]) -> int:
        return self.api.send_message(
            self.chat_id, render_preview(item, variants), approval_keyboard(item.id)
        )

    def send_alert(self, text: str) -> None:
        self.api.send_message(self.chat_id, _truncate(text))
