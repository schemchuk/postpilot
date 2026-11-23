import logging
import re
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import partial
from html import escape
from urllib.parse import unquote, urlsplit

from psycopg import Error

from postpilot.ai.client import AIError
from postpilot.config import ImapSettings
from postpilot.engagement.drafts import Analysis, ReplyDrafter
from postpilot.engagement.imap import ImapError, message_text, read_inbox
from postpilot.engagement.store import EngagementEvent, EngagementStore
from postpilot.engagement.threads import ThreadsEngagementClient, ThreadsEngagementError
from postpilot.notify.telegram import TelegramApi, TelegramError
from postpilot.sources.github import GitHubClient, GitHubError, RepoEngagement
from postpilot.store.bot_state import BotState, GitHubWatchCounts

_LOG = logging.getLogger(__name__)

_POST_DOMAINS = ("linkedin.com", "threads.com", "threads.net")
_THREADS_DRAFT_CAP = 20
_IMAP_RETRY_DELAY = 5.0
IMAP_ALERT_AFTER = 3


def comment_message(
    event: EngagementEvent,
) -> tuple[str, dict[str, list[list[dict[str, str]]]]]:
    actor = escape((event.actor or "Unknown actor")[:180])
    channel = escape(event.channel)
    comment = escape((event.text or "")[:1600])
    draft = escape((event.reply_draft or "Draft unavailable")[:1600])
    buttons = []
    if event.reply_status != "SENT":
        buttons.append(
            {"text": "Another variant", "callback_data": f"eng:redraft:{event.id}"}
        )
        if (
            event.channel == "threads"
            and event.kind == "comment"
            and (event.reply_draft or "").strip()
        ):
            buttons.append({"text": "Send", "callback_data": f"eng:send:{event.id}"})
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
        "inline_keyboard": [buttons] if buttons else []
    }


def collect_linkedin(
    settings: ImapSettings,
    state: BotState,
    store: EngagementStore,
    drafter: ReplyDrafter,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Read LinkedIn emails, tolerating short IMAP outages.

    One IMAP failure is retried once in the same run. If the retry fails too,
    the run only alerts after IMAP_ALERT_AFTER failed runs in a row; the cursor
    is untouched, so the next run reads the skipped emails.
    """
    read = partial(_read_linkedin, settings, state, store, drafter)
    try:
        try:
            found = read()
        except ImapError:
            _LOG.warning("linkedin: IMAP read failed, retrying once")
            sleep(_IMAP_RETRY_DELAY)
            found = read()
    except ImapError:
        failures = state.record_imap_failure()
        if failures >= IMAP_ALERT_AFTER:
            raise
        _LOG.warning(
            "linkedin: IMAP read failed in %d run(s) in a row, alert after %d",
            failures,
            IMAP_ALERT_AFTER,
        )
        return 0
    state.reset_imap_failures()
    return found


def _read_linkedin(
    settings: ImapSettings,
    state: BotState,
    store: EngagementStore,
    drafter: ReplyDrafter,
) -> int:
    found = 0
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
            inserted = store.insert_if_new(
                "linkedin",
                result.kind,
                f"imap:{validity}:{uid}",
                result.actor,
                result.comment_text,
                result.post_url,
                result.reply_draft,
                occurred.replace(tzinfo=UTC) if occurred.tzinfo is None else occurred,
            )
            if inserted is not None:
                found += 1
        state.set_imap_cursor(validity, uid)
    return found


def collect_threads(
    client: ThreadsEngagementClient, store: EngagementStore, drafter: ReplyDrafter
) -> int:
    found = 0
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
            inserted = store.insert_if_new(
                "threads",
                "comment",
                reply.id,
                reply.username,
                reply.text,
                reply.permalink,
                draft,
                reply.timestamp,
            )
            if inserted is not None:
                found += 1
            known.add(reply.id)
            remaining -= 1
            if remaining == 0:
                break
        if remaining == 0:
            break
    if failed:
        raise ThreadsEngagementError("Could not read all Threads conversations")
    return found


def collect_github(
    client: GitHubClient, owner: str, state: BotState, store: EngagementStore
) -> int:
    found = 0
    watch = state.get_github_watch()
    failed = False
    for repo in client.list_public_repos(owner):
        repo_id = str(repo.id)
        previous = watch.get(repo_id)
        counts = GitHubWatchCounts(stars=repo.stargazers_count, forks=repo.forks_count)
        if previous == counts:
            continue
        baseline = previous is None
        try:
            lists: list[tuple[str, list[RepoEngagement]]] = []
            if counts["stars"] != (previous["stars"] if previous else 0):
                lists.append(("star", client.stargazers(owner, repo.name)))
            if counts["forks"] != (previous["forks"] if previous else 0):
                lists.append(("fork", client.forks(owner, repo.name)))
            for kind, entries in lists:
                refs = [f"{kind}:{repo_id}:{entry.id}" for entry in entries]
                known = store.known_refs("github", refs)
                for ref, entry in zip(refs, entries, strict=True):
                    if ref not in known:
                        inserted = store.insert_if_new(
                            "github",
                            kind,
                            ref,
                            entry.login,
                            None,
                            repo.html_url,
                            None,
                            entry.occurred_at,
                            notified=baseline,
                        )
                        if inserted is not None:
                            found += 1
            state.set_github_watch(repo_id, counts)
        except (GitHubError, Error):
            failed = True
    if failed:
        raise GitHubError("Could not process all GitHub repositories")
    return found


def _github_message(event: EngagementEvent) -> str:
    login = event.actor or "Unknown actor"
    actor = escape(login)
    if re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
        actor = f'<a href="https://github.com/{login}">{actor}</a>'
    try:
        repo = urlsplit(event.post_url or "").path.rstrip("/").rsplit("/", 1)[-1]
    except ValueError:
        repo = ""
    action = "⭐" if event.kind == "star" else "🍴"
    verb = "starred" if event.kind == "star" else "forked"
    return f"{action} {actor} {verb} <b>{escape(unquote(repo) or 'Unknown repository')}</b>"


def notify_pending(
    store: EngagementStore, api: TelegramApi, chat_id: int
) -> tuple[dict[str, int], bool]:
    notified: dict[str, int] = {}
    failed = False
    events = store.list_unnotified()
    for event in events:
        message_id = None
        if event.kind in ("comment", "star", "fork"):
            if event.kind == "comment":
                text, keyboard = comment_message(event)
            else:
                text, keyboard = _github_message(event), None
            try:
                message_id = api.send_message(
                    chat_id, text, keyboard, parse_mode="HTML"
                )
                notified[event.channel] = notified.get(event.channel, 0) + 1
            except TelegramError:
                failed = True
                continue
        try:
            store.mark_notified(event.id, message_id)
        except Error:
            failed = True
    return notified, failed


def poll_engagement_sources(
    sources: Mapping[str, Callable[[], int]],
    store: EngagementStore,
    api: TelegramApi,
    chat_id: int,
    *,
    source_failed: bool = False,
) -> None:
    failed = source_failed
    found = {}
    for source, collect in sources.items():
        try:
            found[source] = collect()
        except Exception as exc:  # noqa: BLE001 - isolate failures and keep source data private
            _LOG.warning("%s failed (%s)", source, type(exc).__name__)
            failed = True
    try:
        notified, delivery_failed = notify_pending(store, api, chat_id)
    except Exception:  # noqa: BLE001 - keep source data private
        failed = True
    else:
        failed |= delivery_failed
        for source, count in found.items():
            _LOG.info(
                "%s: found %d, notified %d", source, count, notified.get(source, 0)
            )
    if failed:
        raise RuntimeError("Engagement source or notification delivery failed")
