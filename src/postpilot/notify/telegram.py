import json
import logging
from collections.abc import Iterable
from typing import Any
from uuid import UUID

import httpx

from postpilot.channels.linkedin import LINKEDIN_TEXT_CHANNELS
from postpilot.channels.paused import is_paused
from postpilot.core.models import (
    Item,
    Language,
    Publication,
    PublicationStatus,
    Variant,
)
from postpilot.video.publications import LINKEDIN_VIDEO_CHANNELS
from postpilot.video.store import VideoRender

_MESSAGE_LIMIT = 4096
_VIDEO_LIMIT = 50_000_000
_BOT_URL = "https://api.telegram.org/bot"
_FILE_URL = "https://api.telegram.org/file/bot"


class TelegramError(RuntimeError):
    pass


def _exclude_bot_requests(record: logging.LogRecord) -> bool:
    # HTTPX includes the full credential-bearing URL in its request log.
    return not any(url in record.getMessage() for url in (_BOT_URL, _FILE_URL))


logging.getLogger("httpx").addFilter(_exclude_bot_requests)


def video_preview_markup(render_id: UUID, linkedin: bool) -> dict[str, Any]:
    targets = _linkedin_targets(LINKEDIN_VIDEO_CHANNELS)
    return {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": f"vapprove:{render_id}"},
                {"text": "Reject", "callback_data": f"vreject:{render_id}"},
            ],
            [
                {
                    "text": f"Also on {', '.join(targets)}: {'yes' if linkedin else 'no'}",
                    "callback_data": f"vlinkedin:{render_id}",
                }
            ],
        ]
    }


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
        reply_to_message_id: int | None = None,
    ) -> int:
        payload: dict[str, object] = {"chat_id": chat_id, "text": text}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        result = self._request("sendMessage", **payload)
        if (
            isinstance(result, dict)
            and type(message_id := result.get("message_id")) is int
        ):
            return message_id
        raise TelegramError("Telegram sendMessage returned an invalid message id")

    def get_file(self, file_id: str) -> str:
        result = self._request("getFile", file_id=file_id)
        if isinstance(result, dict) and isinstance(
            path := result.get("file_path"), str
        ):
            return path
        raise TelegramError("Telegram getFile returned an invalid path")

    def download_file(self, path: str) -> bytes:
        get = httpx.get if self._client is None else self._client.get
        try:
            response = get(f"{_FILE_URL}{self._token}/{path}", follow_redirects=False)
            response.raise_for_status()
            return response.content
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError):
            pass
        raise TelegramError("Telegram file download failed")

    def send_photo(self, chat_id: int, photo: bytes) -> None:
        self._request(
            "sendPhoto", {"photo": ("cover.png", photo, "image/png")}, chat_id=chat_id
        )

    def send_video(
        self,
        chat_id: int,
        video: bytes | str,
        caption: str = "",
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, object] = {"chat_id": chat_id, "caption": caption}
        if isinstance(video, str):
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            self._request("sendVideo", video=video, **payload)
            return
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        self._request(
            "sendVideo",
            {"video": ("preview.mp4", video, "video/mp4")},
            supports_streaming="true",
            **payload,
        )

    def send_document_ref(self, chat_id: int, file_id: str) -> None:
        self._request("sendDocument", chat_id=chat_id, document=file_id)

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

    def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: dict[str, Any]
    ) -> None:
        self._request(
            "editMessageReplyMarkup",
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
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


def _linkedin_targets(channels: dict[str, Language]) -> list[str]:
    return [
        f"LinkedIn {lang.upper()}"
        for name, lang in channels.items()
        if not is_paused(name)
    ]


def _preview_heading(item: Item, *, reels: bool = False) -> str:
    targets = [*_linkedin_targets(LINKEDIN_TEXT_CHANNELS), "Threads"]
    if reels:
        targets.extend(("Instagram", "Facebook", "YouTube", "TikTok"))
    return f"Item {item.id.hex[:8]} · {item.source}\nChannels: {', '.join(targets)}"


def _variant_label(lang: Language) -> str:
    if lang == "ua" and is_paused("linkedin-ua"):
        return "UA (not published: LinkedIn UA paused)"
    return lang.upper()


def render_preview(
    item: Item, variants: Iterable[Variant], *, source_details: str = ""
) -> str:
    preview = _preview_heading(item)
    entries = [
        (f"\n\n{_variant_label(variant.lang)}\n", variant.text) for variant in variants
    ]
    footer = (
        f"\n\n{source_details}" if source_details and item.source == "github" else ""
    )
    remaining = (
        _MESSAGE_LIMIT
        - len(preview)
        - len(footer)
        - sum(len(header) for header, _ in entries)
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
    return preview + footer


def approval_keyboard(
    item_id: UUID, *, regenerate: bool = True, edit: bool = True
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
                if (regenerate or action != "regen") and (edit or action != "edit")
            ]
        ]
    }


def send_callback_result(
    api: TelegramApi, chat_id: object, message_id: object, text: str
) -> None:
    if type(chat_id) is not int:
        return
    try:
        if type(message_id) is int:
            api.send_message(chat_id, text, reply_to_message_id=message_id)
        else:
            api.send_message(chat_id, text)
    except TelegramError:
        logging.getLogger(__name__).warning("Telegram callback reply failed")


class Notifier:
    def __init__(self, api: TelegramApi, chat_id: int) -> None:
        self.api = api
        self.chat_id = chat_id

    def send_preview(
        self,
        item: Item,
        variants: Iterable[Variant],
        *,
        source_details: str = "",
        reels: bool = False,
    ) -> int:
        markup = approval_keyboard(item.id, regenerate=item.source != "telegram")
        if item.source in {"telegram", "own_video"}:
            heading = _preview_heading(item, reels=reels)
            if item.source == "own_video" and source_details:
                heading += f"\n{source_details}"
            sections = [
                f"{_variant_label(variant.lang)}\n{variant.text}"
                for variant in variants
            ]
            preview = "\n\n".join([heading, *sections])
            if len(preview) > _MESSAGE_LIMIT:
                # Each variant fits on its own. Offer approval after both are sent.
                for section in sections[:-1]:
                    self.api.send_message(self.chat_id, f"{heading}\n\n{section}")
                preview = f"{heading}\n\n{sections[-1]}"
        else:
            preview = render_preview(item, variants, source_details=source_details)
        return self.api.send_message(self.chat_id, preview, markup)

    def send_project_preview(
        self,
        item_id: UUID,
        cover: bytes | None,
        heading: str,
        post_de: str | None,
        threads: str | None,
    ) -> int:
        if cover is not None:
            self.api.send_photo(self.chat_id, cover)
        markup = {
            "inline_keyboard": [
                [
                    {"text": "Approve", "callback_data": f"papprove:{item_id}"},
                    {"text": "Reject", "callback_data": f"preject:{item_id}"},
                ]
            ]
        }
        sections = [heading]
        if post_de is not None:
            sections.append(f"LinkedIn DE\n{post_de}")
        if threads is not None:
            sections.append(f"Threads\n{threads}")
        preview = "\n\n".join(sections)
        if len(preview.encode("utf-16-le")) // 2 <= _MESSAGE_LIMIT:
            return self.api.send_message(self.chat_id, preview, markup)
        # Keep every character when the preview needs multiple Telegram messages.
        for start in range(0, len(preview), 2000):
            end = start + 2000
            message_id = self.api.send_message(
                self.chat_id,
                preview[start:end],
                markup if end >= len(preview) else None,
            )
        return message_id

    def send_publish_failure(
        self, item: Item, publications: Iterable[Publication]
    ) -> None:
        lines = [
            f"{row.channel}: {(row.error or '')[:300]}"
            for row in publications
            if row.status == PublicationStatus.FAILED and not is_paused(row.channel)
        ]
        if not lines:
            return
        heading = f"Publishing failed · {str(item.id)[:8]} · {item.source}"
        markup = {
            "inline_keyboard": [
                [
                    {"text": "Retry", "callback_data": f"retry:{item.id}"},
                    {"text": "Skip", "callback_data": f"skipfail:{item.id}"},
                ]
            ]
        }
        try:
            self.api.send_message(self.chat_id, "\n".join([heading, *lines]), markup)
        except TelegramError:
            logging.getLogger(__name__).warning(
                "Telegram publishing failure notification failed"
            )

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
            video_preview_markup(render.id, render.linkedin),
        )

    def send_alert(self, text: str) -> None:
        self.api.send_message(self.chat_id, _truncate(text))

    def send_tiktok_draft(self, caption: str) -> None:
        heading = "tiktok: in your TikTok inbox, finish in the TikTok app."
        if not caption:
            self.send_alert(f"{heading}\nNo caption.")
            return
        note = (
            f"Caption is {len(caption)} chars, TikTok allows 2200."
            if len(caption) > 2200
            else ""
        )
        heading += "\nCaption to copy:"
        message = f"{heading}\n{caption}"
        if note:
            message += f"\n{note}"
        if len(message.encode("utf-16-le")) // 2 <= _MESSAGE_LIMIT:
            self.send_alert(message)
            return

        self.send_alert(heading)
        start = size = 0
        for index, character in enumerate(caption):
            width = 2 if ord(character) > 0xFFFF else 1
            if size + width > _MESSAGE_LIMIT:
                self.send_alert(caption[start:index])
                start, size = index, 0
            size += width
        self.send_alert(caption[start:])
        if note:
            self.send_alert(note)

    def send_renewal_prompt(self, provider: str, url: str) -> None:
        label = {"linkedin": "LinkedIn", "threads": "Threads", "tiktok": "TikTok"}.get(
            provider, provider
        )
        text = f"Renew {label} token"
        self.api.send_message(
            self.chat_id,
            f"{text}. This link expires in 30 minutes.",
            {"inline_keyboard": [[{"text": text, "url": url}]]},
        )
