from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import escape
from urllib.parse import urlsplit

from psycopg import Error

from postpilot.ai.client import AIError
from postpilot.config import ImapSettings
from postpilot.engagement.drafts import Analysis, ReplyDrafter
from postpilot.engagement.imap import message_text, read_inbox
from postpilot.engagement.store import EngagementEvent, EngagementStore
from postpilot.notify.telegram import TelegramApi, TelegramError
from postpilot.store.bot_state import BotState


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
                and (host == "linkedin.com" or host.endswith(".linkedin.com"))
            ):
                buttons.insert(0, {"text": "Open post", "url": event.post_url})
    return f"{actor} · {channel}\n\n<i>{comment}</i>\n\n<code>{draft}</code>", {
        "inline_keyboard": [buttons]
    }


def poll_linkedin(
    settings: ImapSettings,
    state: BotState,
    store: EngagementStore,
    drafter: ReplyDrafter,
    api: TelegramApi,
    chat_id: int,
) -> None:
    failed = False
    try:
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
                    occurred.replace(tzinfo=UTC)
                    if occurred.tzinfo is None
                    else occurred,
                )
            state.set_imap_cursor(validity, uid)
    except Exception:  # noqa: BLE001 - drain the notification queue after source errors
        # Driver exceptions can include mail data or credentials.
        failed = True

    try:
        events = store.list_unnotified()
    except Error:
        raise RuntimeError("Could not read engagement notifications") from None
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
        raise RuntimeError("Engagement source or notification delivery failed")
