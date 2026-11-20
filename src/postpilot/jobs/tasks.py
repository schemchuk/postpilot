import logging
import os
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from functools import partial
from math import ceil
from uuid import UUID

import httpx
from psycopg import Connection, Error
from psycopg.rows import TupleRow

from postpilot.ai.client import ClaudeClient
from postpilot.channels.base import TransientChannelError
from postpilot.channels.facebook_reels import FacebookReelsChannel
from postpilot.channels.instagram_reels import InstagramReelsChannel
from postpilot.channels.registry import build_channels
from postpilot.config import ConfigError, HeyGenSettings, Settings
from postpilot.core.publisher import Publisher
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.engagement.drafts import ReplyDrafter
from postpilot.engagement.job import (
    collect_linkedin,
    collect_threads,
    poll_engagement_sources,
)
from postpilot.engagement.store import EngagementStore
from postpilot.engagement.threads import ThreadsEngagementClient
from postpilot.links.shortio import ShortioClient
from postpilot.links.store import LinkStore
from postpilot.notify.approval import ApprovalHandler
from postpilot.notify.poller import poll_once
from postpilot.notify.renewal import RenewalHandler
from postpilot.notify.telegram import Notifier, TelegramApi
from postpilot.sources.github import GitHubClient, GitHubError
from postpilot.sources.series import SeriesStore
from postpilot.sources.telegram import TelegramInbox
from postpilot.store.bot_state import BotState
from postpilot.store.repo import ItemRepo
from postpilot.tokens import service
from postpilot.tokens.base import TokenManager, days_left
from postpilot.tokens.crypto import TokenCipher
from postpilot.tokens.facebook import PAGE_TOKEN_STORAGE_LIFETIME, token_is_valid
from postpilot.tokens.instagram import token_expiry
from postpilot.tokens.linkedin import LinkedInTokenManager
from postpilot.tokens.store import TokenStore
from postpilot.tokens.threads import TOKEN_LIFETIME, ThreadsTokenManager
from postpilot.video.heygen import HeyGenClient
from postpilot.video.pipeline import render_one
from postpilot.video.publications import VideoPublicationStore
from postpilot.video.publisher import VideoChannel, publish_one
from postpilot.video.store import VideoStore

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _managers(
    settings: Settings, store: TokenStore, client: httpx.Client
) -> dict[str, TokenManager]:
    managers: dict[str, TokenManager] = {}
    if (linkedin := settings.linkedin) is not None:
        managers["linkedin"] = LinkedInTokenManager(
            linkedin.client_id,
            linkedin.client_secret,
            linkedin.redirect_uri,
            list(linkedin.scopes),
            store,
            client,
            _now,
        )
    if (threads := settings.threads) is not None:
        managers["threads"] = ThreadsTokenManager(
            threads.app_id,
            threads.app_secret,
            threads.redirect_uri,
            store,
            client,
            _now,
        )
    return managers


def _read_token(
    settings: Settings, conn: Connection[TupleRow], provider: str
) -> str | None:
    token = TokenStore(conn, TokenCipher(settings.token_encryption_key)).get(provider)
    if token is None or days_left(token.expires_at, _now()) <= 0:
        return None
    return token.access_token


def _publisher(
    settings: Settings, repo: ItemRepo, client: httpx.Client
) -> Publisher | None:
    read_token = partial(_read_token, settings, repo.conn)

    channels = build_channels(settings, read_token, client)
    if not channels:
        logger.warning("no channels configured")
        return None
    return Publisher(
        repo, channels, token_guard=lambda provider: bool(read_token(provider))
    )


def _publish_items(publisher: Publisher, item_ids: Iterable[UUID]) -> None:
    failed = False
    for item_id in item_ids:
        try:
            report = publisher.publish_item(item_id)
        except IllegalTransition:
            item = publisher.repo.get_item(item_id)
            if item is None or item.state != ItemState.PUBLISHED:
                raise
        else:
            failed |= any(
                result.outcome == "failed" for result in report.channels.values()
            )
    if failed:
        raise RuntimeError("Channel publishing failed")


def poll_engagement(settings: Settings, conn: Connection[TupleRow]) -> None:
    token_lookup_failed = False
    try:
        token = _read_token(settings, conn, "threads")
    except Exception:  # noqa: BLE001 - isolate token lookup from LinkedIn and Telegram
        token = None
        token_lookup_failed = True

    if settings.imap is None and not token and not token_lookup_failed:
        logger.warning("No engagement sources configured")
        return
    store = EngagementStore(conn)
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        sources: list[Callable[[], None]] = []
        if settings.imap is not None or token:
            if not settings.anthropic_api_key:
                raise ConfigError("ANTHROPIC_API_KEY is required for engagement")
            drafter = ReplyDrafter(ClaudeClient(settings.anthropic_api_key))
            if settings.imap is not None:
                sources.append(
                    partial(
                        collect_linkedin, settings.imap, BotState(conn), store, drafter
                    )
                )
            if token:
                sources.append(
                    partial(
                        collect_threads,
                        ThreadsEngagementClient(client, token),
                        store,
                        drafter,
                    )
                )

        api = TelegramApi(settings.telegram_bot_token, client=client)
        poll_engagement_sources(
            sources,
            store,
            api,
            settings.telegram_chat_id,
            source_failed=token_lookup_failed,
        )


def poll_telegram(settings: Settings, conn: Connection[TupleRow]) -> None:
    repo = ItemRepo(conn)
    store = TokenStore(conn, TokenCipher(settings.token_encryption_key))
    api = TelegramApi(settings.telegram_bot_token)

    def release_held_items(provider: str) -> None:
        publisher = _publisher(settings, repo, client)
        if publisher is not None:
            _publish_items(publisher, repo.list_unfinished_item_ids())

    with httpx.Client() as client:
        renewal = RenewalHandler(
            _managers(settings, store, client),
            settings.token_encryption_key.encode(),
            api,
            release_held_items,
            state_store=repo.bot_state,
        )
        model = (
            ClaudeClient(settings.anthropic_api_key)
            if settings.anthropic_api_key
            else None
        )
        handler = ApprovalHandler(
            repo,
            api,
            settings.telegram_chat_id,
            renewal=renewal,
            inbox=TelegramInbox(repo, api, model),
            video_store=VideoStore(conn),
            engagement=EngagementStore(conn),
            drafter=ReplyDrafter(model) if model is not None else None,
        )
        poll_once(api, handler, repo.bot_state, raise_errors=True)

        item_ids = repo.list_approved_item_ids(source="telegram")
        if item_ids and (publisher := _publisher(settings, repo, client)) is not None:
            _publish_items(publisher, item_ids)


def validate_video_settings(settings: Settings) -> tuple[HeyGenSettings, str]:
    if settings.heygen is None:
        raise ConfigError(
            "HEYGEN_API_KEY, HEYGEN_AVATAR_ID and HEYGEN_VOICE_ID are required"
        )
    if not settings.anthropic_api_key:
        raise ConfigError("ANTHROPIC_API_KEY is required")
    return settings.heygen, settings.anthropic_api_key


def render_video(
    settings: Settings, conn: Connection[TupleRow], *, retry: bool
) -> None:
    heygen_settings, api_key = validate_video_settings(settings)
    heygen = HeyGenClient(heygen_settings)
    try:
        render_one(
            VideoStore(conn),
            ItemRepo(conn),
            heygen,
            ClaudeClient(api_key),
            Notifier(
                TelegramApi(settings.telegram_bot_token), settings.telegram_chat_id
            ),
            _now,
            cap=settings.video_monthly_cap,
            min_balance=settings.video_min_balance_usd,
            retry=retry,
        )
    finally:
        heygen.close()


def publish_reels(
    settings: Settings, conn: Connection[TupleRow], *, retry: bool = False
) -> None:
    if settings.facebook is None and settings.instagram is None:
        return
    store = TokenStore(conn, TokenCipher(settings.token_encryption_key))

    def read_token(provider: str) -> str | None:
        token = store.get(provider)
        return token.access_token if token and token.expires_at > _now() else None

    with httpx.Client() as client:
        channels: list[VideoChannel] = []
        if settings.facebook is not None:
            channels.append(FacebookReelsChannel(settings.facebook, client))
        if settings.instagram is not None:
            channels.append(InstagramReelsChannel(settings.instagram, client))
        publish_one(
            VideoPublicationStore(conn),
            VideoStore(conn),
            channels,
            read_token,
            Notifier(
                TelegramApi(settings.telegram_bot_token), settings.telegram_chat_id
            ).send_alert,
            retry=retry,
        )


def check_tokens(settings: Settings, conn: Connection[TupleRow]) -> None:
    if (
        settings.linkedin is None
        and settings.threads is None
        and settings.facebook is None
        and settings.instagram is None
    ):
        return
    state = ItemRepo(conn).bot_state
    store = TokenStore(conn, TokenCipher(settings.token_encryption_key))
    notifier = Notifier(
        TelegramApi(settings.telegram_bot_token), settings.telegram_chat_id
    )
    now = _now()
    with httpx.Client() as client:
        for provider, config in (
            ("facebook", settings.facebook),
            ("instagram", settings.instagram),
        ):
            if config is None:
                continue
            token = store.get(provider)
            message = None
            if token is not None and provider == "instagram":
                remaining = days_left(token.expires_at, now)
                if remaining <= 0:
                    message = "token missing or invalid"
                elif remaining <= 14:
                    message = (
                        f"token expires in {ceil(remaining)} day(s); re-import manually"
                    )
            try:
                if token is None or not token_is_valid(
                    config, client, token.access_token
                ):
                    message = (
                        "Page token missing or invalid"
                        if provider == "facebook"
                        else "token missing or invalid"
                    )
            except TransientChannelError:
                logger.warning("%s token validation unavailable", provider)
            if message is not None:
                state.send_token_alert_once(
                    provider,
                    now.date(),
                    partial(notifier.send_alert, f"{provider}: {message}"),
                )
        managers = _managers(settings, store, client)
        report = service.check_tokens(
            managers.values(), notifier.send_alert, state, now
        )
        for provider, status in report.statuses.items():
            if status in {"expiring", "expired", "refresh_failed", "missing"}:
                state.send_renewal_prompt_once(
                    provider,
                    now.date(),
                    partial(
                        service.issue_renewal_prompt,
                        managers[provider],
                        settings.token_encryption_key.encode(),
                        state,
                        notifier,
                        now,
                    ),
                )


def publish_due(settings: Settings, conn: Connection[TupleRow]) -> None:
    repo = ItemRepo(conn)
    with httpx.Client() as client:
        publisher = _publisher(settings, repo, client)
        if publisher is not None:
            _publish_items(
                publisher,
                repo.list_unfinished_item_ids() + repo.list_approved_item_ids(),
            )


def threads_import_token(settings: Settings, conn: Connection[TupleRow]) -> None:
    token = os.getenv("THREADS_ACCESS_TOKEN")
    if not token or not token.strip():
        raise ValueError("THREADS_ACCESS_TOKEN is required")
    store = TokenStore(conn, TokenCipher(settings.token_encryption_key))
    store.save("threads", token, _now() + TOKEN_LIFETIME)


def facebook_import_token(settings: Settings, conn: Connection[TupleRow]) -> None:
    token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN")
    if not token or not token.strip():
        raise ValueError("FACEBOOK_PAGE_ACCESS_TOKEN is required")
    TokenStore(conn, TokenCipher(settings.token_encryption_key)).save(
        "facebook", token, _now() + PAGE_TOKEN_STORAGE_LIFETIME
    )


def instagram_import_token(settings: Settings, conn: Connection[TupleRow]) -> None:
    token = os.getenv("INSTAGRAM_ACCESS_TOKEN")
    if not token or not token.strip():
        raise ValueError("INSTAGRAM_ACCESS_TOKEN is required")
    if settings.instagram is None:
        raise ConfigError("INSTAGRAM_BUSINESS_ACCOUNT_ID is required")
    now = _now()
    with httpx.Client() as client:
        expires_at = token_expiry(settings.instagram, client, token, now)
    if expires_at <= now:
        raise ValueError("Instagram token has expired")
    TokenStore(conn, TokenCipher(settings.token_encryption_key)).save(
        "instagram", token, expires_at
    )


def snapshot_traffic(settings: Settings, conn: Connection[TupleRow]) -> None:
    store = LinkStore(conn)
    repos = SeriesStore(conn).posted_repos()
    links = store.list_links()
    if not repos and not links:
        return

    day = _now().date()
    succeeded = False
    with httpx.Client() as client:
        github = GitHubClient(
            settings.github_owner, settings.github_token, client=client
        )
        for repo in sorted(repos):
            try:
                traffic = github.traffic(repo, day=day)
                if traffic is not None:
                    store.upsert_traffic(repo, day, traffic)
                    succeeded = True
                    continue
            except (GitHubError, Error):
                pass
            logger.warning("Traffic snapshot failed for %s", repo)

    shortio = ShortioClient(settings.shortio) if links and settings.shortio else None
    try:
        for link in links:
            try:
                clicks = shortio.total_clicks(link.link_id) if shortio else None
                if clicks is not None:
                    store.upsert_clicks(link.link_id, day, clicks)
                    succeeded = True
                    continue
            except Error:
                pass
            logger.warning("Click snapshot failed for %s", link.link_id)
    finally:
        if shortio is not None:
            shortio.close()

    if not succeeded:
        raise RuntimeError("All traffic and click snapshots failed")


def not_implemented() -> int:
    print("not implemented yet")
    return 0
