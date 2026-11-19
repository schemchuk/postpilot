import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from functools import partial
from uuid import UUID

import httpx
from psycopg import Connection
from psycopg.rows import TupleRow

from postpilot.ai.client import ClaudeClient
from postpilot.channels.registry import build_channels
from postpilot.config import Settings
from postpilot.core.publisher import Publisher
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.notify.approval import ApprovalHandler
from postpilot.notify.poller import poll_once
from postpilot.notify.renewal import RenewalHandler
from postpilot.notify.telegram import Notifier, TelegramApi
from postpilot.sources.telegram import TelegramInbox
from postpilot.store.repo import ItemRepo
from postpilot.tokens import service
from postpilot.tokens.base import TokenManager, days_left
from postpilot.tokens.crypto import TokenCipher
from postpilot.tokens.linkedin import LinkedInTokenManager
from postpilot.tokens.store import TokenStore

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _managers(
    settings: Settings, store: TokenStore, client: httpx.Client
) -> dict[str, TokenManager]:
    config = settings.linkedin
    if config is None:
        return {}
    manager = LinkedInTokenManager(
        config.client_id,
        config.client_secret,
        config.redirect_uri,
        list(config.scopes),
        store,
        client,
        _now,
    )
    return {manager.provider: manager}


def _publisher(settings: Settings, repo: ItemRepo) -> Publisher | None:
    channels = build_channels(settings)
    if not channels:
        logger.warning("no channels configured")
        return None
    store = TokenStore(repo.conn, TokenCipher(settings.token_encryption_key))

    def usable(provider: str) -> bool:
        token = store.get(provider)
        return token is not None and days_left(token.expires_at, _now()) > 0

    return Publisher(repo, channels, token_guard=usable)


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


def poll_telegram(settings: Settings, conn: Connection[TupleRow]) -> None:
    repo = ItemRepo(conn)
    store = TokenStore(conn, TokenCipher(settings.token_encryption_key))
    api = TelegramApi(settings.telegram_bot_token)

    def release_held_items(provider: str) -> None:
        publisher = _publisher(settings, repo)
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
        )
        poll_once(api, handler, repo.bot_state, raise_errors=True)

    item_ids = repo.list_approved_item_ids(source="telegram")
    if item_ids and (publisher := _publisher(settings, repo)) is not None:
        _publish_items(publisher, item_ids)


def check_tokens(settings: Settings, conn: Connection[TupleRow]) -> None:
    if settings.linkedin is None:
        return
    state = ItemRepo(conn).bot_state
    store = TokenStore(conn, TokenCipher(settings.token_encryption_key))
    notifier = Notifier(
        TelegramApi(settings.telegram_bot_token), settings.telegram_chat_id
    )
    now = _now()
    with httpx.Client() as client:
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
    publisher = _publisher(settings, repo)
    if publisher is not None:
        _publish_items(
            publisher, repo.list_unfinished_item_ids() + repo.list_approved_item_ids()
        )


def not_implemented() -> int:
    print("not implemented yet")
    return 0
