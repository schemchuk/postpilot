import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg import Error

from postpilot.ai.client import AIError
from postpilot.core.limits import LINKEDIN_TEXT_LIMIT
from postpilot.core.models import Language
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.engagement.drafts import ReplyDrafter
from postpilot.engagement.job import comment_message
from postpilot.engagement.store import EngagementStore
from postpilot.notify.renewal import RenewalHandler
from postpilot.notify.telegram import Notifier, TelegramApi, TelegramError
from postpilot.sources.telegram import TelegramInbox
from postpilot.store.repo import EditTooLong, ItemRepo
from postpilot.video.store import VideoStore

_LOG = logging.getLogger(__name__)

_AWAITING = ItemState.AWAITING_APPROVAL
_ACTIONS = {
    "approve": (ItemState.APPROVED, "Approved"),
    "skip": (ItemState.SKIPPED, "Skipped"),
    "regen": (ItemState.DRAFT, "Regenerating"),
}


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _reply[**P](method: Callable[P, object], *args: P.args, **kwargs: P.kwargs) -> None:
    try:
        method(*args, **kwargs)
    except TelegramError:
        _LOG.warning("Telegram callback reply failed")


class ApprovalHandler:
    def __init__(
        self,
        repo: ItemRepo,
        api: TelegramApi,
        allowed_user_id: int,
        *,
        on_regenerate: Callable[[UUID], None] | None = None,
        renewal: RenewalHandler | None = None,
        inbox: TelegramInbox | None = None,
        video_store: VideoStore | None = None,
        engagement: EngagementStore | None = None,
        drafter: ReplyDrafter | None = None,
    ) -> None:
        self.engagement = engagement
        self.drafter = drafter
        self.renewal = renewal
        self.inbox = inbox
        self.video_store = video_store
        self.repo = repo
        self.api = api
        self.allowed_user_id = allowed_user_id
        self.on_regenerate = on_regenerate

    def handle_update(self, update: dict[str, Any]) -> None:
        is_callback = "callback_query" in update
        event = _object(update.get("callback_query" if is_callback else "message"))
        sender = _object(event.get("from")).get("id")
        if type(sender) is not int or sender != self.allowed_user_id:
            return

        if is_callback:
            self._handle_callback(event, sender)
        elif isinstance(event.get("text"), str):
            chat_id = _object(event.get("chat")).get("id")
            if event["text"].startswith("/renew"):
                message_id = event.get("message_id")
                if (
                    self.renewal is not None
                    and type(chat_id) is int
                    and type(message_id) is int
                ):
                    self.renewal.handle_message(chat_id, message_id, event["text"])
                return
            if type(chat_id) is int:
                self._handle_text(
                    sender, chat_id, event["text"], update.get("update_id")
                )

    def _handle_text(
        self, sender: int, chat_id: int, text: str, update_id: object
    ) -> None:
        if self.inbox is None:
            item = self.repo.apply_pending_edit(sender, text)
        else:
            if type(update_id) is not int or not text.strip() or text.startswith("/"):
                return
            if self.repo.has_inbox_update(update_id):
                return
            try:
                item = self.repo.apply_pending_edit(sender, text, update_id=update_id)
            except EditTooLong:
                self.api.send_message(
                    chat_id, f"Edit exceeds the {LINKEDIN_TEXT_LIMIT}-character limit."
                )
                return
            if item is None:
                self.inbox.handle_text(update_id, chat_id, text)
                return
        if item is not None:
            Notifier(self.api, chat_id).send_preview(
                item, self.repo.get_variants(item.id)
            )

    def _handle_callback(self, event: dict[str, Any], sender: int) -> None:
        data, callback_id = event.get("data"), event.get("id")
        if not isinstance(data, str) or not isinstance(callback_id, str):
            return
        action, _, raw_id = data.rpartition(":")
        if action not in _ACTIONS and action not in {
            "edit",
            "vapprove",
            "vreject",
            "eng:redraft",
        }:
            return
        try:
            item_id = UUID(raw_id)
        except ValueError:
            return

        message = _object(event.get("message"))
        chat_id = _object(message.get("chat")).get("id")
        message_id = message.get("message_id")
        if action == "eng:redraft":
            if type(chat_id) is int and chat_id == sender and type(message_id) is int:
                self._redraft(item_id, chat_id, message_id, callback_id)
            return
        if action in {"vapprove", "vreject"}:
            store = self.video_store
            if store is None:
                return
            keyboard = _object(message.get("reply_markup")).get("inline_keyboard", [])
            if not isinstance(message.get("video"), dict) or not any(
                _object(button).get("callback_data") == data
                for row in keyboard
                for button in row
            ):
                _reply(self.api.answer_callback_query, callback_id, "Already handled")
                return
            approve = action == "vapprove"
            changed = (
                store.approve(item_id, sender, datetime.now(UTC))
                if approve
                else store.reject(item_id, sender)
            )
            _reply(
                self.api.answer_callback_query,
                callback_id,
                None if changed else "Already handled",
            )
            if changed and type(chat_id) is int and type(message_id) is int:
                label = "Video approved" if approve else "Video rejected"
                _reply(self.api.edit_message_caption, chat_id, message_id, label)
            return
        item = self.repo.get_item(item_id)
        if item is None or item.state != _AWAITING:
            _reply(self.api.answer_callback_query, callback_id, "Already handled")
            return

        if action == "regen" and item.source == "telegram":
            self.api.answer_callback_query(callback_id, "Regeneration is unavailable")
            return

        if action == "edit":
            if type(chat_id) is not int:
                return
            variants = self.repo.get_variants(item_id)
            lang: Language = (
                "de"
                if item.source == "telegram"
                or any(variant.lang == "de" for variant in variants)
                else "ua"
            )
            self.repo.bot_state.set_pending_edit(sender, item_id, lang)
            _reply(self.api.answer_callback_query, callback_id)
            _reply(self.api.send_message, chat_id, f"Send the new {lang.upper()} text.")
            return

        state, label = _ACTIONS[action]
        try:
            self.repo.transition(
                item_id,
                state,
                expected_state=_AWAITING,
                approved_by=sender if action == "approve" else None,
            )
        except IllegalTransition:
            _reply(self.api.answer_callback_query, callback_id, "Already handled")
            return

        if action == "regen" and self.on_regenerate is not None:
            self.on_regenerate(item_id)
        _reply(self.api.answer_callback_query, callback_id)
        if type(chat_id) is int and type(message_id) is int:
            _reply(self.api.edit_message_text, chat_id, message_id, label)

    def _redraft(
        self, id: UUID, chat_id: int, message_id: int, callback_id: str
    ) -> None:
        store, drafter = self.engagement, self.drafter
        if store is None or drafter is None:
            _reply(self.api.answer_callback_query, callback_id, "Redraft unavailable")
            return
        comment = store.get(id)
        if (
            comment is None
            or comment.kind != "comment"
            or not comment.text
            or comment.telegram_message_id != message_id
        ):
            _reply(self.api.answer_callback_query, callback_id, "Comment unavailable")
            return
        try:
            draft = drafter.redraft(comment.text, comment.reply_draft or "")
        except AIError:
            draft = None
        if not draft:
            _reply(self.api.answer_callback_query, callback_id, "Redraft unavailable")
            return
        try:
            store.set_reply_draft(id, draft)
        except Error:
            raise RuntimeError("Could not save engagement draft") from None
        text, keyboard = comment_message(replace(comment, reply_draft=draft))
        _reply(
            self.api.edit_message_text,
            chat_id,
            message_id,
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        _reply(self.api.answer_callback_query, callback_id)
