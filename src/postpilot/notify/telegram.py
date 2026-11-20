import json
import logging
from collections.abc import Iterable
from typing import Any
from uuid import UUID

import httpx

from postpilot.core.models import Item, Variant
from postpilot.video.store import VideoRender

_MESSAGE_LIMIT = 4096
_VIDEO_LIMIT = 50_000_000
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

    def _request(
        self,
        method: str,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        /,
        **payload: object,
    ) -> Any:
        url = f"{_BOT_URL}{self._token}/{method}"
        post = httpx.post if self._client is None else self._client.post
        try:
            if files is None:
                response = post(url, json=payload, follow_redirects=False)
            else:
                response = post(
                    url,
                    data=payload,
                    files=files,
                    timeout=120.0,
                    follow_redirects=False,
                )
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
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        *,
        parse_mode: str | None = None,
    ) -> int:
        payload: dict[str, object] = {"chat_id": chat_id, "text": text}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = self._request("sendMessage", **payload)
        if (
            isinstance(result, dict)
            and type(message_id := result.get("message_id")) is int
        ):
            return message_id
        raise TelegramError("Telegram sendMessage returned an invalid message id")

    def send_video(
        self, chat_id: int, video: bytes, caption: str, reply_markup: dict[str, Any]
    ) -> None:
        self._request(
            "sendVideo",
            {"video": ("preview.mp4", video, "video/mp4")},
            chat_id=chat_id,
            caption=caption,
            supports_streaming="true",
            reply_markup=json.dumps(reply_markup),
        )

    def send_document(
        self, chat_id: int, filename: str, content: bytes, caption: str | None = None
    ) -> None:
        payload: dict[str, object] = {"chat_id": chat_id}
        if caption is not None:
            payload["caption"] = caption
        self._request(
            "sendDocument",
            {"document": (filename, content, "text/markdown")},
            **payload,
        )

    def edit_message_caption(self, chat_id: int, message_id: int, caption: str) -> None:
        self._request(
            "editMessageCaption",
            chat_id=chat_id,
            message_id=message_id,
            caption=caption,
            reply_markup={"inline_keyboard": []},
        )

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self._request("deleteMessage", chat_id=chat_id, message_id=message_id)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._request("editMessageText", **payload)

    def answer_callback_query(self, id: str, text: str | None = None) -> None:
        payload = {} if text is None else {"text": text}
        self._request("answerCallbackQuery", callback_query_id=id, **payload)

    def get_updates(
        self, offset: int, timeout: int = 0, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        payload = {"offset": offset, "timeout": timeout}
        if limit is not None:
            payload["limit"] = limit
        result = self._request("getUpdates", **payload)
        if not isinstance(result, list):
            raise TelegramError("Telegram getUpdates returned an invalid result")
        return result


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


def approval_keyboard(
    item_id: UUID, *, regenerate: bool = True
) -> dict[str, list[list[dict[str, str]]]]:
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
                if regenerate or action != "regen"
            ]
        ]
    }


class Notifier:
    def __init__(self, api: TelegramApi, chat_id: int) -> None:
        self.api = api
        self.chat_id = chat_id

    def send_preview(self, item: Item, variants: Iterable[Variant]) -> int:
        markup = approval_keyboard(item.id, regenerate=item.source != "telegram")
        if item.source == "telegram":
            heading = f"Item {item.id.hex[:8]} · {item.source}"
            sections = [
                f"{variant.lang.upper()}\n{variant.text}" for variant in variants
            ]
            preview = "\n\n".join([heading, *sections])
            if len(preview) > _MESSAGE_LIMIT:
                # Each variant fits on its own. Offer approval after both are sent.
                for section in sections[:-1]:
                    self.api.send_message(self.chat_id, f"{heading}\n\n{section}")
                preview = f"{heading}\n\n{sections[-1]}"
        else:
            preview = render_preview(item, variants)
        return self.api.send_message(self.chat_id, preview, markup)

    def send_video_preview(self, render: VideoRender, video: bytes) -> None:
        if len(video) > _VIDEO_LIMIT:
            self.send_alert(
                f"Video render {render.id} exceeds Telegram's 50 MB upload limit."
            )
            return
        self.api.send_video(
            self.chat_id,
            video,
            "AI-generated avatar video (label burned in) - approve to allow Instagram/Facebook",
            {
                "inline_keyboard": [
                    [
                        {"text": "Approve", "callback_data": f"vapprove:{render.id}"},
                        {"text": "Reject", "callback_data": f"vreject:{render.id}"},
                    ]
                ]
            },
        )

    def send_alert(self, text: str) -> None:
        self.api.send_message(self.chat_id, _truncate(text))

    def send_renewal_prompt(self, provider: str, url: str) -> None:
        label = {"linkedin": "LinkedIn", "threads": "Threads"}.get(provider, provider)
        text = f"Renew {label} token"
        self.api.send_message(
            self.chat_id,
            f"{text}. This link expires in 30 minutes.",
            {"inline_keyboard": [[{"text": text, "url": url}]]},
        )
