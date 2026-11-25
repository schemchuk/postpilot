import logging
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from html import escape
from typing import Any
from uuid import UUID

from psycopg import Error

from postpilot.ai.client import AIError
from postpilot.core.limits import LINKEDIN_TEXT_LIMIT
from postpilot.core.models import Language
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.engagement.drafts import ReplyDrafter
from postpilot.engagement.job import comment_message
from postpilot.engagement.store import EngagementEvent, EngagementStore
from postpilot.engagement.threads import (
    THREADS_REPLY_LIMIT,
    ThreadsReplyClient,
    ThreadsReplyError,
    ThreadsReplyPermissionError,
    UncertainThreadsReply,
)
from postpilot.notify.renewal import RenewalHandler
from postpilot.notify.telegram import (
    Notifier,
    TelegramApi,
    TelegramError,
    send_callback_result,
    video_preview_markup,
)
from postpilot.sources.github import GitHubClient, GitHubError
from postpilot.sources.own_video import OwnVideoInbox
from postpilot.sources.series import (
    ActiveSeriesExists,
    NoActiveSeries,
    QueuedSeriesExists,
    SeriesStore,
)
from postpilot.sources.telegram import TelegramInbox
from postpilot.stats import StatsStore, format_stats
from postpilot.store.bot_state import PROFILE_LIMIT
from postpilot.store.repo import EditTooLong, ItemRepo
from postpilot.trace import trace
from postpilot.video.store import VideoStore

_LOG = logging.getLogger(__name__)

_PROFILE_COMMAND = re.compile(
    r"^/(profile_add|profile_clear|profile|advise)(?:@[a-zA-Z0-9_]+)?(?=\s|$)"
)
_SERIES_COMMAND = re.compile(
    r"^/(series_start|series_stop|series_next_cancel|series_next|series)"
    r"(?:@[a-zA-Z0-9_]+)?(?=\s|$)"
)
_SERIES_USAGE = "Usage: /series_start <repo> [parts (2-10)]"
_SERIES_NEXT_USAGE = "Usage: /series_next <repo> [parts (2-10)]"


def _parts_label(parts: int | None) -> str:
    return f"{parts} parts" if parts else "open-ended"


_FAILURE_ACTIONS = {
    "retry": (ItemState.PUBLISHING, "Retrying"),
    "skipfail": (ItemState.SKIPPED, "Skipped"),
}
_VIDEO_ACTIONS = {"vapprove", "vreject", "vlinkedin"}
_AWAITING = ItemState.AWAITING_APPROVAL
_ACTIONS = {
    "approve": (ItemState.APPROVED, "Approved"),
    "skip": (ItemState.SKIPPED, "Skipped"),
    "regen": (ItemState.DRAFT, "Regenerating"),
}


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _reply[**P](method: Callable[P, object], *args: P.args, **kwargs: P.kwargs) -> bool:
    try:
        method(*args, **kwargs)
    except TelegramError:
        _LOG.warning("Telegram callback reply failed")
        return False
    return True


class ApprovalHandler:
    def __init__(
        self,
        repo: ItemRepo,
        api: TelegramApi,
        allowed_user_id: int,
        *,
        on_regenerate: Callable[[UUID], None] | None = None,
        on_retry: Callable[[UUID], None] | None = None,
        recovered_regenerations: set[UUID] | None = None,
        series: SeriesStore | None = None,
        github_factory: Callable[[], GitHubClient] | None = None,
        github_owner: str = "",
        renewal: RenewalHandler | None = None,
        inbox: TelegramInbox | None = None,
        own_video: OwnVideoInbox | None = None,
        video_store: VideoStore | None = None,
        engagement: EngagementStore | None = None,
        drafter: ReplyDrafter | None = None,
        stats: StatsStore | None = None,
        threads_reply_client: Callable[[], ThreadsReplyClient | None] | None = None,
    ) -> None:
        self.advice_chat_id: int | None = None
        self.recovered_regenerations = recovered_regenerations or set()
        self.series = series
        self.github_factory = github_factory
        self.github_owner = github_owner
        self.stats = stats
        self.threads_reply_client = threads_reply_client
        self.engagement = engagement
        self.drafter = drafter
        self.renewal = renewal
        self.inbox = inbox
        self.own_video = own_video
        self.video_store = video_store
        self.repo = repo
        self.api = api
        self.allowed_user_id = allowed_user_id
        self.on_regenerate = on_regenerate
        self.on_retry = on_retry

    def handle_update(self, update: dict[str, Any]) -> None:
        is_callback = "callback_query" in update
        event = _object(update.get("callback_query" if is_callback else "message"))
        sender = _object(event.get("from")).get("id")
        if type(sender) is not int or sender != self.allowed_user_id:
            return

        update_id = update.get("update_id")
        if is_callback:
            self._handle_callback(event, sender, update_id)
        elif (
            self.own_video is not None
            and type(update_id) is int
            and type(chat := _object(event.get("chat")).get("id")) is int
            and self.own_video.message(update_id, chat, event)
        ):
            return
        elif isinstance(event.get("text"), str):
            chat_id = _object(event.get("chat")).get("id")
            if re.match(r"^/stats(?:@[a-zA-Z0-9_]+)?(?:\s|$)", event["text"]):
                if self.stats is not None and type(chat_id) is int:
                    _reply(
                        self.api.send_message,
                        chat_id,
                        format_stats(self.stats.read(datetime.now(UTC))),
                        parse_mode="HTML",
                    )
                return
            if match := _PROFILE_COMMAND.match(event["text"]):
                if type(chat_id) is int:
                    self._handle_profile_command(
                        chat_id, match[1], event["text"][match.end() :].strip()
                    )
                return
            if match := _SERIES_COMMAND.match(event["text"]):
                if type(chat_id) is int:
                    self._handle_series_command(
                        chat_id, match[1], event["text"][match.end() :].split()
                    )
                return
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
                reply_id = _object(event.get("reply_to_message")).get("message_id")
                if (
                    chat_id == sender
                    and type(reply_id) is int
                    and not event["text"].lstrip().startswith("/")
                    and self._edit_engagement_reply(chat_id, reply_id, event["text"])
                ):
                    return
                self._handle_text(
                    sender, chat_id, event["text"], update.get("update_id")
                )

    def _handle_series_command(
        self, chat_id: int, command: str, args: list[str]
    ) -> None:
        store = self.series
        try:
            if store is None:
                reply = "Series commands are unavailable."
            elif command in ("series_start", "series_next"):
                reply = self._start_series(store, args, queue=command == "series_next")
            elif args:
                reply = f"Usage: /{command}"
            elif command == "series_next_cancel":
                cancelled = store.cancel_queued()
                reply = (
                    f"Queued series about {cancelled.repo} cancelled."
                    if cancelled
                    else "No queued series."
                )
            elif command == "series_stop":
                stopped = store.stop()
                reply = (
                    f"Series about {stopped.repo} stopped after part {stopped.part - 1}."
                    if stopped
                    else "No active series."
                )
                if stopped and (following := store.get_active()) is not None:
                    reply += f"\nNext series started: {following.repo}."
            elif active := store.get_active():
                reply = f"Active series: {active.repo}, next part {active.part}"
                if active.total_parts is not None:
                    reply += f"/{active.total_parts}"
                if (queued := store.get_queued()) is not None:
                    reply += f"\nQueued next: {queued.repo}, " + _parts_label(
                        queued.total_parts
                    )
            else:
                reply = "No active series."
                if posted := sorted(store.posted_repos()):
                    reply += f"\nPosted repos: {', '.join(posted)}."
                reply += "\nStart one with /series_start <repo> [parts]."
        except GitHubError:
            reply = "GitHub check failed, series not started."
        except Error:
            reply = "Series storage operation failed."
        _reply(self.api.send_message, chat_id, reply)

    def _start_series(self, store: SeriesStore, args: list[str], *, queue: bool) -> str:
        usage = _SERIES_NEXT_USAGE if queue else _SERIES_USAGE
        if not 1 <= len(args) <= 2 or not re.fullmatch(
            r"[A-Za-z0-9._-]{1,100}", args[0]
        ):
            return usage
        parts = None
        if len(args) == 2:
            try:
                parts = int(args[1], 10)
            except ValueError:
                return usage
            if not 2 <= parts <= 10:
                return usage
        try:
            active = store.get_active()
            if queue and active is None:
                raise NoActiveSeries
            if not queue and active is not None:
                raise ActiveSeriesExists
            if queue and store.get_queued() is not None:
                raise QueuedSeriesExists
            if self.github_factory is None:
                raise GitHubError
            meta = self.github_factory().get_repo(args[0])
            if (
                meta is None
                or meta.visibility != "public"
                or meta.is_private
                or meta.archived
                or meta.fork
                or meta.owner.casefold() != self.github_owner.casefold()
            ):
                return usage
            if queue:
                queued = store.queue(meta.name, parts)
                return (
                    f"Series queued: {queued.repo}, {_parts_label(parts)}. "
                    "It starts when the active series ends."
                )
            started = store.start(meta.name, parts)
        except ActiveSeriesExists:
            active = store.get_active()
            name = active.repo if active else args[0]
            return (
                f"A series about {name} is already active. Stop it with /series_stop"
                " or queue the next one with /series_next."
            )
        except NoActiveSeries:
            return "No active series. Start one with /series_start <repo> [parts]."
        except QueuedSeriesExists:
            waiting = store.get_queued()
            name = waiting.repo if waiting else args[0]
            return (
                f"A series about {name} is already queued. "
                "Cancel it with /series_next_cancel."
            )
        return f"Series started: {started.repo}, {_parts_label(parts)}"

    def _handle_profile_command(self, chat_id: int, command: str, text: str) -> None:
        if command == "advise":
            self.advice_chat_id = chat_id
            return

        state = self.repo.bot_state
        try:
            if command == "profile_clear":
                state.delete_linkedin_profile()
                reply = "Profile cleared."
            elif not text:
                reply = f"Usage: /{command} <text>"
            else:
                length = state.set_linkedin_profile(
                    text, append=command == "profile_add"
                )
                reply = (
                    f"Profile saved ({length} characters)."
                    if length is not None
                    else f"Profile exceeds the {PROFILE_LIMIT:,}-character limit."
                )
        except (Error, ValueError):
            raise RuntimeError("Profile storage operation failed.") from None
        _reply(self.api.send_message, chat_id, reply)

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
        if item is not None and item.source == "own_video" and self.own_video:
            self.own_video.preview(chat_id, item)
        elif item is not None:
            Notifier(self.api, chat_id).send_preview(
                item, self.repo.get_variants(item.id)
            )

    def _handle_callback(
        self, event: dict[str, Any], sender: int, update_id: object
    ) -> None:
        data, callback_id = event.get("data"), event.get("id")
        if not isinstance(data, str) or not isinstance(callback_id, str):
            return
        action, _, raw_id = data.rpartition(":")
        if action not in _ACTIONS and action not in {
            *_FAILURE_ACTIONS,
            "edit",
            "own:gen",
            "own:skip",
            *_VIDEO_ACTIONS,
            "eng:redraft",
            "eng:send",
        }:
            return
        try:
            item_id = UUID(raw_id)
        except ValueError:
            return

        message = _object(event.get("message"))
        chat_id = _object(message.get("chat")).get("id")
        message_id = message.get("message_id")
        if action in _FAILURE_ACTIONS:
            if type(chat_id) is not int or type(message_id) is not int:
                return
            if action == "retry" and self.on_retry is None:
                send_callback_result(self.api, chat_id, message_id, "Retry unavailable")
                return
            state, label = _FAILURE_ACTIONS[action]
            try:
                self.repo.resolve_failed_item(item_id, state, message_id)
            except (IllegalTransition, KeyError):
                send_callback_result(self.api, chat_id, message_id, "Already handled")
                return
            send_callback_result(self.api, chat_id, message_id, label)
            _reply(
                self.api.edit_message_reply_markup,
                chat_id,
                message_id,
                {"inline_keyboard": []},
            )
            if action == "retry" and self.on_retry is not None:
                self.on_retry(item_id)
            return
        own = self.own_video
        if action in {"own:gen", "own:skip"}:
            if own and type(chat_id) is int and type(update_id) is int:
                own.callback(update_id, chat_id, message_id, action, item_id)
            return
        if action in {"eng:redraft", "eng:send"}:
            if type(chat_id) is int and chat_id == sender and type(message_id) is int:
                handler = (
                    self._send_engagement_reply
                    if action == "eng:send"
                    else self._redraft
                )
                handler(item_id, chat_id, message_id)
            return
        if action in _VIDEO_ACTIONS:
            store = self.video_store
            if store is None:
                return
            keyboard = _object(message.get("reply_markup")).get("inline_keyboard", [])
            if not isinstance(message.get("video"), dict) or not any(
                _object(button).get("callback_data") == data
                for row in keyboard
                for button in row
            ):
                send_callback_result(self.api, chat_id, message_id, "Already handled")
                return
            if action == "vlinkedin":
                if type(chat_id) is not int or type(message_id) is not int:
                    send_callback_result(
                        self.api, chat_id, message_id, "Invalid preview"
                    )
                    return
                linkedin = store.toggle_linkedin(item_id)
                if linkedin is None:
                    send_callback_result(
                        self.api, chat_id, message_id, "Already handled"
                    )
                else:
                    _reply(
                        self.api.edit_message_reply_markup,
                        chat_id,
                        message_id,
                        video_preview_markup(item_id, linkedin),
                    )
                return
            approve = action == "vapprove"
            changed = (
                store.approve(item_id, sender, datetime.now(UTC))
                if approve
                else store.reject(item_id, sender)
            )
            if not changed:
                send_callback_result(self.api, chat_id, message_id, "Already handled")
            elif type(chat_id) is int and type(message_id) is int:
                label = "Video approved" if approve else "Video rejected"
                _reply(self.api.edit_message_caption, chat_id, message_id, label)
            return
        item = self.repo.get_item(item_id)
        if item is None or item.state != _AWAITING:
            send_callback_result(self.api, chat_id, message_id, "Already handled")
            return

        # Telegram may replay the callback that was in flight when the poll died.
        if action == "regen" and item_id in self.recovered_regenerations:
            send_callback_result(self.api, chat_id, message_id, "Already handled")
            return

        if (
            action == "regen"
            and item.source == "own_video"
            and own
            and type(chat_id) is int
            and type(update_id) is int
        ):
            own.callback(update_id, chat_id, message_id, action, item_id)
            return
        if action == "regen" and (
            item.source in {"telegram", "own_video"} or self.on_regenerate is None
        ):
            send_callback_result(
                self.api, chat_id, message_id, "Regeneration is unavailable"
            )
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
            trace("approval", "already-handled", item=item_id, action=action)
            send_callback_result(self.api, chat_id, message_id, "Already handled")
            return
        trace("approval", "transition", item=item_id, action=action, state=state)

        if type(chat_id) is int and type(message_id) is int:
            _reply(self.api.edit_message_text, chat_id, message_id, label)
        if action == "regen" and self.on_regenerate is not None:
            self.on_regenerate(item_id)

    def _redraft(self, id: UUID, chat_id: int, message_id: int) -> None:
        store, drafter = self.engagement, self.drafter
        if store is None:
            send_callback_result(self.api, chat_id, message_id, "Redraft unavailable")
            return
        try:
            comment = store.get(id)
        except Error:
            raise RuntimeError("Could not load engagement comment") from None
        if (
            comment is None
            or comment.kind != "comment"
            or comment.telegram_message_id != message_id
        ):
            send_callback_result(self.api, chat_id, message_id, "Comment unavailable")
            return
        if comment.reply_status in ("SENT", "SENDING"):
            send_callback_result(self.api, chat_id, message_id, "Reply already sent")
            return
        if drafter is None or not comment.text:
            send_callback_result(self.api, chat_id, message_id, "Redraft unavailable")
            return
        try:
            draft = drafter.redraft(comment.text, comment.reply_draft or "")
        except AIError:
            draft = None
        if not draft:
            send_callback_result(self.api, chat_id, message_id, "Redraft unavailable")
            return
        if not self._save_engagement_draft(store, comment, draft, chat_id):
            send_callback_result(self.api, chat_id, message_id, "Reply already sent")

    def _edit_engagement_reply(self, chat_id: int, message_id: int, text: str) -> bool:
        store = self.engagement
        if store is None:
            return False
        try:
            comment = store.find_by_message_id(message_id)
        except Error:
            raise RuntimeError("Could not load engagement comment") from None
        if comment is None:
            return False
        draft = text.strip()
        if comment.reply_status in ("SENT", "SENDING"):
            _reply(self.api.send_message, chat_id, "Reply already sent")
        elif not draft:
            _reply(self.api.send_message, chat_id, "Reply is empty")
        elif comment.channel == "threads" and len(draft) > THREADS_REPLY_LIMIT:
            _reply(self.api.send_message, chat_id, "Reply exceeds 500 characters")
        elif not self._save_engagement_draft(store, comment, draft, chat_id):
            _reply(self.api.send_message, chat_id, "Reply already sent")
        return True

    def _save_engagement_draft(
        self, store: EngagementStore, comment: EngagementEvent, draft: str, chat_id: int
    ) -> bool:
        try:
            changed = store.set_reply_draft(comment.id, draft)
        except Error:
            raise RuntimeError("Could not save engagement draft") from None
        if changed:
            self._edit_engagement_message(chat_id, replace(comment, reply_draft=draft))
        return changed

    def _edit_engagement_message(
        self, chat_id: int, comment: EngagementEvent, sent_link: str | None = None
    ) -> bool:
        message_id = comment.telegram_message_id
        if message_id is None:
            return False
        text, keyboard = comment_message(comment)
        if comment.reply_status == "SENT":
            text += f"\n\nSent: {escape(sent_link)}" if sent_link else "\n\nSent"
        return _reply(
            self.api.edit_message_text,
            chat_id,
            message_id,
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    def _send_engagement_reply(self, id: UUID, chat_id: int, message_id: int) -> None:
        store = self.engagement
        if store is None:
            return
        try:
            comment = store.get(id)
            if (
                comment is None
                or comment.kind != "comment"
                or comment.channel != "threads"
                or comment.telegram_message_id != message_id
            ):
                return
            claimed = store.claim_reply(id, datetime.now(UTC))
            if claimed is None:
                current = store.get(id)
                answer: str | None = (
                    "Already sent"
                    if current and current.reply_status == "SENT"
                    else "Sending in progress"
                )
            else:
                answer = self._publish_engagement_reply(store, claimed, chat_id)
        except Error:
            raise RuntimeError("Could not store engagement reply") from None
        if answer is not None:
            send_callback_result(self.api, chat_id, message_id, answer)

    def _publish_engagement_reply(
        self, store: EngagementStore, comment: EngagementEvent, chat_id: int
    ) -> str | None:
        client = self.threads_reply_client() if self.threads_reply_client else None
        if client is None:
            store.mark_reply_failed(comment.id)
            return "Threads token unavailable"
        container = comment.reply_container_id
        try:
            status = client.container_status(container) if container else None
            remote_id = None
            if status == "IN_PROGRESS":
                return "Send uncertain, press again in 5 minutes"
            if status != "PUBLISHED":
                if status != "FINISHED" or container is None:
                    container = client.create_reply_container(
                        comment.source_ref, comment.reply_draft or ""
                    )
                    store.save_reply_container(comment.id, container)
                remote_id = client.publish(container)
        except UncertainThreadsReply:
            return "Send uncertain, press again in 5 minutes"
        except ThreadsReplyError as exc:
            store.mark_reply_failed(comment.id)
            if isinstance(exc, ThreadsReplyPermissionError):
                return "Token lacks threads_manage_replies, re-issue it (runbook)"
            return "Send failed"

        store.mark_reply_sent(comment.id, remote_id, datetime.now(UTC))
        link = client.permalink(remote_id) if remote_id else None
        edited = self._edit_engagement_message(
            chat_id, replace(comment, reply_status="SENT"), link or comment.post_url
        )
        return None if edited else "Sent"
