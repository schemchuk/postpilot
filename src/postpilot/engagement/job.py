from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import partial
from html import escape
from urllib.parse import urlsplit

from psycopg import Error

from postpilot.ai.client import AIError
from postpilot.config import ImapSettings
from postpilot.engagement.drafts import Analysis, ReplyDrafter
from postpilot.engagement.imap import message_text, read_inbox
from postpilot.engagement.store import EngagementEvent, EngagementStore
from postpilot.engagement.threads import ThreadsEngagementClient, ThreadsEngagementError
from postpilot.notify.telegram import TelegramApi, TelegramError
from postpilot.store.bot_state import BotState

_POST_DOMAINS = ("linkedin.com", "threads.com", "threads.net")
_THREADS_DRAFT_CAP = 20


def comment_message(
    event: EngagementEvent,
) -> tuple[str, dict[str, list[list[dict[str, str]]]]]:
    actor = escape((event.actor or "Unknown actor")[:180])
    channel = escape(event.channel)
    comment = escape((event.text or "")[:1600])
    draft = escape((event.reply_draft or "Draft unavailable")[:1600])
    buttons = [{"text": "Another variant", "callback_data": f"eng:redraft:{event.id}"}]
    if event.post_url and not any(
        char.isspace() or char == "\\" or ord(char) < 32 for char in event.post_url
    ):
        try:
            url = urlsplit(event.post_url)
            host = url.hostname or ""
        except ValueError:
            pass
        else:
            if (
                url.scheme == "https"
                and url.username is None
                and any(
                    host == domain or host.endswith(f".{domain}")
                    for domain in _POST_DOMAINS
                )
            ):
                buttons.insert(0, {"text": "Open post", "url": event.post_url})
    return f"{actor} · {channel}\n\n<i>{comment}</i>\n\n<code>{draft}</code>", {
        "inline_keyboard": [buttons]
    }


def collect_linkedin(
    settings: ImapSettings,
    state: BotState,
    store: EngagementStore,
    drafter: ReplyDrafter,
) -> None:
    for validity, uid, message in read_inbox(settings, state.get_imap_cursor()):
        if message is not None:
            body = message_text(message)
            try:
                result = drafter.analyze(body)
            except AIError:
                result = Analysis()
            now = datetime.now(UTC)
            try:
                occurred = parsedate_to_datetime(str(message["Date"]))
            except (ValueError, OverflowError):
                occurred = now
            store.insert_if_new(
                "linkedin",
                result.kind,
                f"imap:{validity}:{uid}",
                result.actor,
                result.comment_text,
                result.post_url,
                result.reply_draft,
                occurred.replace(tzinfo=UTC) if occurred.tzinfo is None else occurred,
            )
        state.set_imap_cursor(validity, uid)


def collect_threads(
    client: ThreadsEngagementClient, store: EngagementStore, drafter: ReplyDrafter
) -> None:
    remaining = _THREADS_DRAFT_CAP
    failed = False
    for media_id in store.recent_threads_posts(datetime.now(UTC) - timedelta(days=14)):
        try:
            replies = client.conversation(media_id)
        except ThreadsEngagementError:
            failed = True
            continue
        known = store.known_refs("threads", [reply.id for reply in replies])
        for reply in replies:
            if reply.id in known:
                continue
            try:
                draft = drafter.draft(reply.text)
            except AIError:
                draft = None
            store.insert_if_new(
                "threads",
                "comment",
                reply.id,
                reply.username,
                reply.text,
                reply.permalink,
                draft,
                reply.timestamp,
            )
            known.add(reply.id)
            remaining -= 1
            if remaining == 0:
                break
        if remaining == 0:
            break
    if failed:
        raise ThreadsEngagementError("Could not read all Threads conversations")


def notify_pending(store: EngagementStore, api: TelegramApi, chat_id: int) -> None:
    failed = False
    events = store.list_unnotified()
    for event in events:
        message_id = None
        if event.kind == "comment":
            text, keyboard = comment_message(event)
            try:
                message_id = api.send_message(
                    chat_id, text, keyboard, parse_mode="HTML"
                )
            except TelegramError:
                failed = True
                continue
        try:
            store.mark_notified(event.id, message_id)
        except Error:
            failed = True
    if failed:
        raise RuntimeError("Engagement notification delivery failed")


def poll_engagement_sources(
    sources: Iterable[Callable[[], None]],
    store: EngagementStore,
    api: TelegramApi,
    chat_id: int,
    *,
    source_failed: bool = False,
) -> None:
    failed = source_failed
    actions: list[Callable[[], None]] = [
        *sources,
        partial(notify_pending, store, api, chat_id),
    ]
    for action in actions:
        try:
            action()
        except Exception:  # noqa: BLE001 - isolate failures and keep source data private
            failed = True
    if failed:
        raise RuntimeError("Engagement source or notification delivery failed")
