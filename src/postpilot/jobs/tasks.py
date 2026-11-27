import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from functools import partial
from math import ceil
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from psycopg import Connection, Error
from psycopg.rows import TupleRow

from postpilot.ai.advisor import AdvisorModel, generate_advice
from postpilot.ai.client import ClaudeClient, TextModel
from postpilot.ai.humanizer import humanize_post
from postpilot.ai.prompts import (
    POST_SYSTEM_PROMPT,
    SERIES_SYSTEM_PROMPT,
    generation_message,
)
from postpilot.channels.base import (
    ChannelError,
    TransientChannelError,
)
from postpilot.channels.facebook_reels import FacebookReelsChannel
from postpilot.channels.factory import (
    FactoryAssets,
    FactoryLinkedInChannel,
    FactoryReelsChannel,
    FactoryThreadsChannel,
)
from postpilot.channels.instagram_reels import InstagramReelsChannel
from postpilot.channels.linkedin import LINKEDIN_TEXT_CHANNELS, LinkedInChannel
from postpilot.channels.linkedin_video import LinkedInRenderChannel
from postpilot.channels.own_video_reels import OwnVideoReelsChannel
from postpilot.channels.paused import is_paused
from postpilot.channels.registry import build_channels, build_own_video_channels
from postpilot.channels.threads import ThreadsChannel
from postpilot.channels.tiktok import TikTokChannel
from postpilot.channels.youtube import YouTubeChannel
from postpilot.config import ConfigError, HeyGenSettings, Settings
from postpilot.core.approval import (
    ProjectApprovalError,
    ProjectDailyLimitError,
    ProjectDisabledError,
)
from postpilot.core.limits import LINKEDIN_TEXT_LIMIT
from postpilot.core.models import Item
from postpilot.core.publisher import ChannelResult, Publisher
from postpilot.core.repo_safety import check_repo, finalize_links
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.engagement.drafts import ReplyDrafter
from postpilot.engagement.job import (
    collect_github,
    collect_linkedin,
    collect_threads,
    poll_engagement_sources,
)
from postpilot.engagement.store import EngagementStore
from postpilot.engagement.threads import ThreadsEngagementClient, ThreadsReplyClient
from postpilot.links.shortio import ShortioClient, tracked_link
from postpilot.links.store import LinkStore
from postpilot.notify.approval import ApprovalHandler
from postpilot.notify.poller import drain_inbox
from postpilot.notify.renewal import RenewalHandler
from postpilot.notify.telegram import Notifier, TelegramApi, TelegramError
from postpilot.projects import Projects, ProjectsConfigError
from postpilot.sources.github import (
    PROMPT_COMMIT_LIMIT,
    GitHubClient,
    GitHubError,
    pick_project,
)
from postpilot.sources.own_video import OwnVideoInbox
from postpilot.sources.project_release import scan_projects
from postpilot.sources.series import SeriesStore
from postpilot.sources.telegram import TelegramInbox
from postpilot.stats import StatsStore
from postpilot.store.bot_state import BotState
from postpilot.store.own_videos import OwnVideo, OwnVideoStore
from postpilot.store.project_items import ProjectItemStore
from postpilot.store.repo import ItemRepo
from postpilot.store.tiktok_settings import TikTokSettingsStore
from postpilot.store.update_inbox import UpdateInbox
from postpilot.store.weekly import WeeklyDraft, WeeklyStore
from postpilot.tokens import service
from postpilot.tokens.base import TokenManager, days_left
from postpilot.tokens.crypto import TokenCipher
from postpilot.tokens.facebook import PAGE_TOKEN_STORAGE_LIFETIME, token_is_valid
from postpilot.tokens.instagram import token_expiry
from postpilot.tokens.linkedin import LinkedInTokenManager
from postpilot.tokens.store import TokenStore
from postpilot.tokens.threads import TOKEN_LIFETIME, ThreadsTokenManager
from postpilot.tokens.tiktok import TikTokTokenError, TikTokTokenManager
from postpilot.tokens.youtube import (
    REFRESH_TOKEN_STORAGE_EXPIRY,
    YouTubeRefreshError,
    YouTubeTokenManager,
)
from postpilot.trace import error_fields, trace
from postpilot.video.heygen import HeyGenClient
from postpilot.video.pipeline import render_one
from postpilot.video.publications import LINKEDIN_VIDEO_CHANNELS, VideoPublicationStore
from postpilot.video.publisher import RenderChannel, VideoChannel, alert, publish_one
from postpilot.video.store import VideoStore
from postpilot.video.tiktok import TikTokClient, UploadPlan

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
    if settings.tiktok is not None:
        managers["tiktok"] = TikTokTokenManager(settings.tiktok, store, client, _now)
    return managers


def _read_token(
    settings: Settings, conn: Connection[TupleRow], provider: str
) -> str | None:
    token = TokenStore(conn, TokenCipher(settings.token_encryption_key)).get(provider)
    if token is None or days_left(token.expires_at, _now()) <= 0:
        return None
    return token.access_token


_TEXT_TARGETS = tuple(
    name
    for name in (*LINKEDIN_TEXT_CHANNELS, ThreadsChannel.name)
    if not is_paused(name)
)
_INSTAGRAM_TARGET = InstagramReelsChannel.name
_REELS_TARGETS = tuple(
    name
    for name in (
        _INSTAGRAM_TARGET,
        FacebookReelsChannel.name,
        YouTubeChannel.name,
        TikTokChannel.name,
    )
    if not is_paused(name)
)


def _excluded_own_video_channels(store: OwnVideoStore, item_id: UUID) -> set[str]:
    video = store.get_by_item(item_id)
    if video is None:
        return set()
    if not video.has_description:
        return {*LINKEDIN_TEXT_CHANNELS, ThreadsChannel.name}
    return set() if video.reels_eligible else set(_REELS_TARGETS)


def _publisher(
    settings: Settings, repo: ItemRepo, client: httpx.Client
) -> Publisher | None:
    read_token = partial(_read_token, settings, repo.conn)

    channels = build_channels(settings, read_token, client)
    if not channels:
        logger.warning("no channels configured")
        return None
    store = OwnVideoStore(repo.conn)
    api = TelegramApi(settings.telegram_bot_token, client=client)

    def get_video(item_id: UUID) -> OwnVideo:
        video = store.get_by_item(item_id)
        if video is None:
            raise ChannelError("Own video is unavailable")
        return video

    def fetch_video(item_id: UUID) -> bytes:
        return api.download_file(api.get_file(get_video(item_id).file_id))

    own_video_channels = build_own_video_channels(
        channels,
        fetch_video=fetch_video,
        ai_generated=lambda item_id: get_video(item_id).ai_generated,
    )
    configured = {channel.name for channel in own_video_channels}
    missing_text_targets = tuple(
        name for name in _TEXT_TARGETS if name not in configured
    )
    return Publisher(
        repo,
        channels,
        token_guard=lambda provider: bool(read_token(provider)),
        own_video_channels=own_video_channels,
        own_video_held_channels=(*missing_text_targets, *_REELS_TARGETS),
        own_video_excluded_channels=partial(_excluded_own_video_channels, store),
    )


class _ChannelPublishingFailed(RuntimeError):
    pass


def _publish_items(
    publisher: Publisher,
    item_ids: Iterable[UUID],
    *,
    on_result: Callable[[UUID, str, ChannelResult], None] | None = None,
    notifier: Notifier | None = None,
    on_project_hold: Callable[[UUID, ProjectApprovalError], None] | None = None,
) -> None:
    failed = False
    for item_id in item_ids:
        try:
            report = publisher.publish_item(item_id)
        except ProjectApprovalError as error:
            logger.warning("Project item %s held: %s", item_id, error)
            if on_project_hold is not None:
                on_project_hold(item_id, error)
        except IllegalTransition:
            item = publisher.repo.get_item(item_id)
            if item is None or item.state != ItemState.PUBLISHED:
                raise
        else:
            if notifier is not None and report.transitioned_to_failed:
                item = publisher.repo.get_item(item_id)
                if item is not None:
                    notifier.send_publish_failure(
                        item, publisher.repo.list_publications(item_id)
                    )
            for name, result in report.channels.items():
                failed |= result.outcome == "failed"
                if on_result is not None:
                    on_result(item_id, name, result)
    if failed:
        raise _ChannelPublishingFailed("Channel publishing failed")


def poll_engagement(settings: Settings, conn: Connection[TupleRow]) -> None:
    store = EngagementStore(conn)
    purge_failed = False
    try:
        purged = store.purge_personal_data(_now())
    except Exception:  # noqa: BLE001 - isolate purge failures and keep event data private
        purge_failed = True
        logger.warning("Engagement data purge failed")
    else:
        logger.info("Engagement rows purged: %s", purged)

    token_lookup_failed = False
    try:
        token = _read_token(settings, conn, "threads")
    except Exception:  # noqa: BLE001 - isolate token lookup from LinkedIn and Telegram
        token = None
        token_lookup_failed = True

    if (
        not settings.github_owner
        and settings.imap is None
        and not token
        and not token_lookup_failed
    ):
        logger.warning("No engagement sources configured")
        if purge_failed:
            raise RuntimeError("Engagement data purge failed")
        return
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        sources: dict[str, Callable[[], int]] = {}
        drafts_unavailable = False
        if (settings.imap is not None or token) and not settings.anthropic_api_key:
            if not settings.github_owner:
                raise ConfigError("ANTHROPIC_API_KEY is required for engagement")
            drafts_unavailable = True
            logger.warning("ANTHROPIC_API_KEY is required for comment monitoring")
        if (settings.imap is not None or token) and settings.anthropic_api_key:
            drafter = ReplyDrafter(ClaudeClient(settings.anthropic_api_key))
            if settings.imap is not None:
                sources["linkedin"] = partial(
                    collect_linkedin, settings.imap, BotState(conn), store, drafter
                )
            if token:
                sources["threads"] = partial(
                    collect_threads,
                    ThreadsEngagementClient(client, token),
                    store,
                    drafter,
                )

        if settings.github_owner:
            sources["github"] = partial(
                collect_github,
                GitHubClient(
                    settings.github_owner, settings.github_token, client=client
                ),
                settings.github_owner,
                BotState(conn),
                store,
            )
        api = TelegramApi(settings.telegram_bot_token, client=client)
        poll_engagement_sources(
            sources,
            store,
            api,
            settings.telegram_chat_id,
            source_failed=token_lookup_failed or purge_failed or drafts_unavailable,
        )


def poll_telegram(settings: Settings, conn: Connection[TupleRow]) -> None:
    repo = ItemRepo(conn)
    store = TokenStore(conn, TokenCipher(settings.token_encryption_key))
    api = TelegramApi(settings.telegram_bot_token)

    notifier = Notifier(api, settings.telegram_chat_id)

    def publish(item_ids: Iterable[UUID]) -> None:
        publisher = _publisher(settings, repo, client)
        if publisher is not None:
            _publish_items(publisher, item_ids, notifier=notifier)

    def release_held_items(provider: str) -> None:
        publish(repo.list_unfinished_item_ids())

    def retry_item(item_id: UUID) -> None:
        item = repo.get_item(item_id)
        if item is not None and item.source == "project":
            return
        try:
            publish([item_id])
        except _ChannelPublishingFailed:
            logger.warning(
                "Channel publishing failed after Telegram retry (item %s)", item_id
            )

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
        read_threads_token = partial(_read_token, settings, conn, "threads")

        def threads_reply_client() -> ThreadsReplyClient | None:
            token = read_threads_token()
            return ThreadsReplyClient(client, token) if token else None

        weekly = WeeklyStore(conn)
        recovered = set()
        for interrupted in weekly.list_interrupted():
            assert interrupted.item_id is not None
            try:
                item = repo.transition(
                    interrupted.item_id,
                    ItemState.AWAITING_APPROVAL,
                    expected_state=ItemState.DRAFT,
                )
                recovered.add(item.id)
                try:
                    notifier.send_alert("Regeneration interrupted, previous draft kept")
                finally:
                    _weekly_preview(notifier, repo, item, interrupted)
            except Exception as error:  # noqa: BLE001 - continue processing updates
                logger.warning(
                    "Regeneration recovery failed (%s)", type(error).__name__
                )

        handler = ApprovalHandler(
            repo,
            api,
            settings.telegram_chat_id,
            on_regenerate=partial(
                _regenerate_weekly, settings, repo, weekly, notifier, model
            ),
            on_retry=retry_item,
            recovered_regenerations=recovered,
            series=SeriesStore(conn),
            github_factory=partial(
                GitHubClient,
                settings.github_owner,
                settings.github_token,
                client=client,
            ),
            github_owner=settings.github_owner,
            renewal=renewal,
            inbox=TelegramInbox(repo, api, model),
            own_video=OwnVideoInbox(repo, api, model),
            video_store=VideoStore(conn),
            project_store=ProjectItemStore(conn),
            engagement=EngagementStore(conn),
            stats=StatsStore(conn),
            threads_reply_client=threads_reply_client,
            drafter=ReplyDrafter(model) if model is not None else None,
        )
        drain_inbox(UpdateInbox(conn), handler, raise_errors=True)

        try:
            item_ids = repo.list_approved_item_ids(source="telegram")
            if settings.linkedin is not None:
                item_ids += repo.list_approved_item_ids(source="own_video")
                item_ids += repo.list_unfinished_item_ids(source="own_video")
            if (
                item_ids
                and (publisher := _publisher(settings, repo, client)) is not None
            ):
                _publish_items(publisher, item_ids, notifier=notifier)
        finally:
            if handler.advice_chat_id is not None:
                _send_profile_advice(
                    settings, repo.bot_state, api, handler.advice_chat_id, model, client
                )


def _send_profile_advice(
    settings: Settings,
    state: BotState,
    api: TelegramApi,
    chat_id: int,
    model: AdvisorModel | None,
    client: httpx.Client,
) -> None:
    try:
        profile = state.get_linkedin_profile()
        if not profile:
            api.send_message(chat_id, "No profile stored. Send /profile <text> first.")
        elif model is None:
            api.send_message(chat_id, "Profile advice requires an AI key.")
        else:
            github = GitHubClient(
                settings.github_owner, settings.github_token, client=client
            )
            advice = generate_advice(model, github, settings.github_owner, profile)
            api.send_document(
                chat_id,
                f"profile-advice-{_now().date().isoformat()}.md",
                advice.encode("utf-8"),
                caption="Profile advice",
            )
    except Exception as error:  # noqa: BLE001 - advisory failures must not fail the poll
        logger.warning("Profile advisor failed (%s)", type(error).__name__)
        try:
            api.send_message(chat_id, "Advice failed")
        except Exception as reply_error:  # noqa: BLE001 - best-effort failure reply
            logger.warning(
                "Profile advisor reply failed (%s)", type(reply_error).__name__
            )


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


def _reel_channels(settings: Settings, client: httpx.Client) -> list[VideoChannel]:
    channels: list[VideoChannel] = []
    if settings.facebook is not None:
        channels.append(FacebookReelsChannel(settings.facebook, client))
    if settings.instagram is not None:
        channels.append(InstagramReelsChannel(settings.instagram, client))
    return channels


def _own_video_reel_publisher(
    repo: ItemRepo,
    channels: list[VideoChannel],
    api: TelegramApi,
    read_token: Callable[[str], str | None],
    *,
    tiktok_settings: TikTokSettingsStore | None = None,
) -> Publisher:
    store = OwnVideoStore(repo.conn)
    return Publisher(
        repo,
        [],
        own_video_channels=[
            OwnVideoReelsChannel(
                channel,
                store.get_by_item,
                api,
                read_token,
                caption_limit=2200 if channel.name == _INSTAGRAM_TARGET else None,
                tiktok_choice=tiktok_settings.get
                if tiktok_settings is not None and channel.name == TikTokChannel.name
                else None,
            )
            for channel in channels
        ],
        own_video_held_channels=(*_TEXT_TARGETS, *_REELS_TARGETS),
        own_video_excluded_channels=partial(_excluded_own_video_channels, store),
    )


def publish_reels(
    settings: Settings, conn: Connection[TupleRow], *, retry: bool = False
) -> None:
    store = TokenStore(conn, TokenCipher(settings.token_encryption_key))

    with httpx.Client() as client:
        repo = ItemRepo(conn)
        api = TelegramApi(settings.telegram_bot_token, client=client)
        notifier = Notifier(api, settings.telegram_chat_id)
        notify = notifier.send_alert
        now = _now()

        def daily_notice(key: str, message: str) -> None:
            day = _now().astimezone(ZoneInfo("Europe/Berlin")).date()
            try:
                repo.bot_state.send_daily_once(key, day, partial(notify, message))
            except TelegramError:
                logger.warning("Telegram project notice failed")

        def project_hold(item_id: UUID, error: ProjectApprovalError) -> None:
            if isinstance(error, ProjectDisabledError):
                return  # Disabled projects already have a project-wide notice.
            if isinstance(error, ProjectDailyLimitError):
                daily_notice(f"projects:daily_limit:{item_id}", str(error))
            else:
                daily_notice(
                    f"projects:held:{item_id}",
                    f"Project item {item_id.hex[:8]} held: {error}",
                )

        project_items = ProjectItemStore(conn)
        config_invalid = False
        try:
            projects = Projects.load()
        except ProjectsConfigError as error:
            config_invalid = True
            logger.warning("Project configuration invalid: %s", error)
            daily_notice(
                "projects:config",
                "Project publishing held: config/projects.toml is invalid.",
            )
            projects = Projects()
        for project in projects:
            if not project.enabled:
                daily_notice(
                    f"projects:disabled:{project.key}",
                    f"Project {project.key} is disabled",
                )
        scan_projects(
            projects, client, project_items, notifier, now, token=settings.github_token
        )

        youtube_token: str | None = None
        youtube_checked = False
        tiktok_token: str | None = None
        tiktok_checked = False

        def read_token(provider: str) -> str | None:
            nonlocal youtube_token, youtube_checked, tiktok_token, tiktok_checked
            if provider == YouTubeChannel.token_provider:
                if not youtube_checked and settings.youtube is not None:
                    youtube_checked = True
                    try:
                        youtube_token = YouTubeTokenManager(
                            settings.youtube, store, client
                        ).access_token()
                    except YouTubeRefreshError as error:
                        logger.warning("%s", error)
                return youtube_token
            if provider == TikTokChannel.token_provider:
                if not tiktok_checked and settings.tiktok is not None:
                    tiktok_checked = True
                    try:
                        tiktok_token = TikTokTokenManager(
                            settings.tiktok, store, client, _now
                        ).access_token()
                    except TikTokTokenError as error:
                        logger.warning("%s", error)
                return tiktok_token
            token = store.get(provider)
            return token.access_token if token and token.expires_at > _now() else None

        reel_channels = _reel_channels(settings, client)
        channels: list[VideoChannel | RenderChannel] = list(reel_channels)
        if settings.linkedin is not None:
            token_source = partial(read_token, LinkedInChannel.token_provider)
            channels.extend(
                LinkedInRenderChannel(
                    LinkedInChannel(
                        name,
                        lang,
                        token_source,
                        client,
                        settings.linkedin.version,
                        video_marker=True,
                    ),
                    repo,
                )
                for name, lang in LINKEDIN_VIDEO_CHANNELS.items()
                if not is_paused(name)
            )
        publish_one(
            VideoPublicationStore(conn),
            VideoStore(conn),
            channels,
            read_token,
            notify,
            retry=retry,
        )

        if settings.youtube is not None and not is_paused(YouTubeChannel.name):
            reel_channels.append(YouTubeChannel(client, clock=_now))
        tiktok = None
        tiktok_settings = None
        if settings.tiktok is not None and not is_paused(TikTokChannel.name):
            tiktok = TikTokChannel(client, mode=settings.tiktok.mode, clock=_now)
            if tiktok.mode == "direct":
                tiktok_settings = TikTokSettingsStore(conn)
            reel_channels.append(tiktok)
        permalinks = {channel.name: channel.permalink for channel in reel_channels}

        def notify_result(item_id: UUID, name: str, result: ChannelResult) -> None:
            if result.outcome == "skipped" and result.error is not None:
                alert(notify, f"{name}: skipped, {result.error}.")
            if result.outcome == "published":
                for publication in repo.list_publications(item_id):
                    if (
                        publication.channel == name
                        and publication.remote_id is not None
                    ):
                        if tiktok is not None and name == tiktok.name:
                            caption = tiktok.caption(publication.remote_id)
                            if caption is None:
                                alert(notify, "tiktok: posted")
                            elif tiktok.mode == "direct":
                                alert(
                                    partial(notifier.send_tiktok_draft, fallback=True),
                                    caption,
                                )
                            else:
                                alert(notifier.send_tiktok_draft, caption)
                        else:
                            link = permalinks[name](publication.remote_id)
                            alert(notify, f"{name}: published {link}")
                        break

        own_video_failed = False
        if reel_channels:
            publisher = _own_video_reel_publisher(
                repo, reel_channels, api, read_token, tiktok_settings=tiktok_settings
            )
            item_ids = repo.list_approved_item_ids(source="own_video")
            item_ids += repo.list_unfinished_item_ids(source="own_video")
            try:
                _publish_items(
                    publisher,
                    dict.fromkeys(item_ids),
                    on_result=notify_result,
                    notifier=notifier,
                )
            except _ChannelPublishingFailed:
                own_video_failed = True

        due = [] if config_invalid else project_items.list_due_item_ids(_now())
        if due:
            assets = FactoryAssets(project_items, client, projects)
            targets: list[
                FactoryLinkedInChannel | FactoryThreadsChannel | FactoryReelsChannel
            ] = [
                FactoryReelsChannel(
                    channel,
                    read_token,
                    assets,
                    caption_limit=2200 if channel.name == _INSTAGRAM_TARGET else None,
                    use_release_video=channel.name
                    in {YouTubeChannel.name, TikTokChannel.name},
                    tiktok_choice=tiktok_settings.get
                    if tiktok_settings is not None
                    and channel.name == TikTokChannel.name
                    else None,
                )
                for channel in reel_channels
            ]
            if settings.linkedin is not None:
                targets.extend(
                    FactoryLinkedInChannel(
                        LinkedInChannel(
                            name, lang, token_source, client, settings.linkedin.version
                        ),
                        assets,
                    )
                    for name, lang in LINKEDIN_VIDEO_CHANNELS.items()
                    if lang == "de" and not is_paused(name)
                )
            targets.append(
                FactoryThreadsChannel(partial(read_token, "threads"), client, assets)
            )
            permalinks.update({channel.name: channel.permalink for channel in targets})

            _publish_items(
                Publisher(
                    repo,
                    [],
                    project_channels=list(targets),
                    project_item=project_items.get_by_item,
                    projects=projects,
                    project_exclusions=lambda reel: (
                        {"threads"} if reel.threads_skipped else set()
                    ),
                    project_lock=project_items.lock_item,
                    now=_now,
                    token_guard=lambda provider: bool(read_token(provider)),
                ),
                due,
                on_result=notify_result,
                notifier=notifier,
                on_project_hold=project_hold,
            )
        if own_video_failed:
            raise _ChannelPublishingFailed("Channel publishing failed")


def check_tokens(settings: Settings, conn: Connection[TupleRow]) -> None:
    if (
        settings.linkedin is None
        and settings.threads is None
        and settings.facebook is None
        and settings.instagram is None
        and settings.youtube is None
        and settings.tiktok is None
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
        if settings.youtube is not None:
            try:
                YouTubeTokenManager(settings.youtube, store, client).access_token()
            except YouTubeRefreshError:
                state.send_token_alert_once(
                    "youtube",
                    now.date(),
                    partial(
                        notifier.send_alert,
                        "youtube: token missing or refresh failed; re-run local consent "
                        'from docs/runbook.md ("YouTube Shorts setup"), then run '
                        "postpilot youtube-import-token",
                    ),
                )
        managers = _managers(settings, store, client)
        report = service.check_tokens(
            managers.values(), notifier.send_alert, state, now
        )
        for provider, status in report.statuses.items():
            if provider == "tiktok" and status == "missing":
                continue
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


_DRAFT_FIELDS = ("decision", "repo", "post_de", "post_uk", "reason")
_DRAFT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        **{field: {"type": "string"} for field in _DRAFT_FIELDS},
        "decision": {"type": "string", "enum": ["post", "skip"]},
    },
    "required": list(_DRAFT_FIELDS),
    "additionalProperties": False,
}


class _WeeklyGenerationError(RuntimeError):
    """A fixed, safe label for the generation step that failed."""


def _generate_weekly(
    model: TextModel,
    system: str,
    payload: dict[str, object],
    name: str,
    url: str,
    allowed: bool,
) -> tuple[str, str] | str:
    try:
        response = model.complete(system, generation_message(payload), _DRAFT_SCHEMA)
    except Exception:  # noqa: BLE001 - keep model errors out of job alerts
        raise _WeeklyGenerationError("generation (AI request)") from None
    try:
        draft = json.loads(response)
    except (TypeError, ValueError, RecursionError):
        raise _WeeklyGenerationError("generation (invalid JSON)") from None
    if (
        not isinstance(draft, dict)
        or draft.keys() != set(_DRAFT_FIELDS)
        or not all(isinstance(value, str) for value in draft.values())
        or draft["decision"] not in ("post", "skip")
    ):
        raise _WeeklyGenerationError("generation (invalid response)")
    if draft["repo"] != name:
        raise _WeeklyGenerationError("generation (wrong repo)")
    if draft["decision"] == "skip":
        if reason := str(draft["reason"]).strip():
            return reason
        raise _WeeklyGenerationError("generation (missing skip reason)")

    texts = _finalize_weekly_texts((draft["post_de"], draft["post_uk"]), url, allowed)
    trace("draft-weekly", "generated", de_chars=len(texts[0]), ua_chars=len(texts[1]))
    for index, (lang, model_lang) in enumerate((("de", "de"), ("ua", "uk"))):
        try:
            texts[index] = humanize_post(model, texts[index], model_lang, payload)
        except Exception:  # noqa: BLE001 - keep model errors out of job alerts
            raise _WeeklyGenerationError(f"humanizer ({lang})") from None
        trace("draft-weekly", "humanized", lang=lang, chars=len(texts[index]))
    _check_weekly_lengths(texts)
    return texts[0], texts[1]


def _finalize_weekly_texts(
    variants: tuple[str, str], url: str, allowed: bool
) -> list[str]:
    texts = []
    for lang, text in zip(("de", "ua"), variants, strict=True):
        try:
            texts.append(finalize_links(text, url, allowed).strip())
        except ValueError:
            raise _WeeklyGenerationError(f"link validation ({lang})") from None
    _check_weekly_lengths(texts)
    return texts


def _check_weekly_lengths(texts: list[str]) -> None:
    for lang, text in zip(("de", "ua"), texts, strict=True):
        if not text or len(text) > LINKEDIN_TEXT_LIMIT:
            raise _WeeklyGenerationError(f"length ({lang})")


def _weekly_links(
    settings: Settings,
    conn: Connection[TupleRow],
    repo: ItemRepo,
    item: Item,
    name: str,
    url: str,
    allowed: bool,
) -> None:
    if not allowed or settings.shortio is None:
        return
    exact_url = re.compile(r"(?<![\w/])" + re.escape(url) + r"(?![\w./?#%&=~-])")
    shortio = ShortioClient(settings.shortio)
    try:
        for variant in repo.get_variants(item.id):
            if exact_url.search(variant.text):
                short = tracked_link(
                    shortio,
                    LinkStore(conn),
                    url,
                    name,
                    variant.lang,
                    item_id=item.id,
                    strict_storage=item.state == ItemState.DRAFT,
                )
                repo.update_variant_text(
                    item.id,
                    variant.lang,
                    exact_url.sub(short.replace("\\", "\\\\"), variant.text),
                    expected_state=item.state,
                )
                trace(
                    "draft-weekly",
                    "short-link",
                    item=item.id,
                    lang=variant.lang,
                    link=short,
                )
            else:
                trace("draft-weekly", "no-repo-url", item=item.id, lang=variant.lang)
    finally:
        shortio.close()


def _weekly_preview(
    notifier: Notifier, repo: ItemRepo, item: Item, row: WeeklyDraft
) -> None:
    source = row.facts.get("source_details")
    notifier.send_preview(
        item,
        repo.get_variants(item.id),
        source_details=source if isinstance(source, str) else "",
    )


def _regenerate_weekly(
    settings: Settings,
    repo: ItemRepo,
    weekly: WeeklyStore,
    notifier: Notifier,
    model: TextModel | None,
    item_id: UUID,
) -> None:
    row = None
    awaiting, draft = ItemState.AWAITING_APPROVAL, ItemState.DRAFT
    step = "stored facts"
    alert = None
    try:
        try:
            row = weekly.get_by_item(item_id)
            if row is None:
                repo.transition(item_id, awaiting, expected_state=draft)
                notifier.send_alert("Regeneration is unavailable for this item")
                return
            assert row.repo is not None
            facts = row.facts
            if facts.get("prompt") == "planned" or model is None:
                alert = (
                    "Regeneration is unavailable for a planned part"
                    if facts.get("prompt") == "planned"
                    else "Regeneration is unavailable"
                )
                item = repo.transition(item_id, awaiting, expected_state=draft)
            else:
                payload = facts.get("payload")
                if not isinstance(payload, dict):
                    metadata = {
                        "prompt",
                        "repo_url",
                        "link_allowed",
                        "link_reasons",
                        "source_details",
                    }
                    payload = {
                        key: value
                        for key, value in facts.items()
                        if key not in metadata
                    }
                inferred = (
                    "series" if {"part", "previous_parts"} & payload.keys() else "post"
                )
                prompt = facts.get("prompt", inferred)
                system = {"series": SERIES_SYSTEM_PROMPT, "post": POST_SYSTEM_PROMPT}[
                    str(prompt)
                ]
                url = facts.get("repo_url")
                if not isinstance(url, str):
                    url = f"https://github.com/{settings.github_owner}/{row.repo}"
                allowed = facts["link_allowed"] is True
                step = "generation"
                result = _generate_weekly(
                    model, system, payload, row.repo, url, allowed
                )
                if isinstance(result, str):
                    raise _WeeklyGenerationError("model skip")
                step = "storage"
                with weekly.conn.transaction():
                    repo.update_variant_text(
                        item_id, "de", result[0], expected_state=draft
                    )
                    repo.update_variant_text(
                        item_id, "ua", result[1], expected_state=draft
                    )
                    current = repo.get_item(item_id)
                    assert current is not None
                    step = "short links"
                    _weekly_links(
                        settings, weekly.conn, repo, current, row.repo, url, allowed
                    )
                    step = "storage"
                    item = repo.transition(item_id, awaiting, expected_state=draft)
        except Exception as error:  # noqa: BLE001 - restore the previous draft
            logger.warning("Regeneration failed (%s)", type(error).__name__)
            item = repo.transition(item_id, awaiting, expected_state=draft)
            if isinstance(error, _WeeklyGenerationError):
                step = str(error)
            alert = f"Regeneration failed, previous draft kept ({step})"

        if alert:
            try:
                notifier.send_alert(alert)
            except TelegramError:
                logger.warning("Regeneration alert failed (TelegramError)")
        if row is not None:
            _weekly_preview(notifier, repo, item, row)
        else:
            notifier.send_preview(item, repo.get_variants(item.id))
    except Exception as error:  # noqa: BLE001 - allow the poll to handle other updates
        logger.warning("Regeneration follow-up failed (%s)", type(error).__name__)


def draft_weekly(settings: Settings, conn: Connection[TupleRow]) -> None:
    now = _now().astimezone(ZoneInfo("Europe/Berlin"))
    week = now.date() - timedelta(days=now.weekday())
    weekly = WeeklyStore(conn)
    existing = weekly.get(week)
    trace(
        "draft-weekly",
        "week",
        week=week,
        berlin_now=now.isoformat(timespec="minutes"),
        existing=existing.outcome if existing else None,
        item=existing.item_id if existing else None,
    )
    if existing is not None:
        if existing.outcome == "drafted" and existing.item_id is not None:
            repo = ItemRepo(conn)
            item = repo.get_item(existing.item_id)
            if item is not None and item.state == ItemState.AWAITING_APPROVAL:
                with httpx.Client(timeout=30) as client:
                    notifier = Notifier(
                        TelegramApi(settings.telegram_bot_token, client=client),
                        settings.telegram_chat_id,
                    )
                    _weekly_preview(notifier, repo, item, existing)
                print(f"draft-weekly: week {week} preview resent")
                return
        print(f"draft-weekly: week {week} already handled ({existing.outcome})")
        return
    series_store = SeriesStore(conn)
    series = series_store.get_active()
    planned = series_store.planned_part(series) if series is not None else None
    trace(
        "draft-weekly",
        "series",
        active=series.repo if series else None,
        series_id=series.id if series else None,
        part=series.part if series else None,
        total=series.total_parts if series else None,
        planned=planned is not None,
        planned_de_chars=len(planned[0]) if planned else None,
        planned_ua_chars=len(planned[1]) if planned else None,
    )
    api_key = settings.anthropic_api_key
    if planned is None and not api_key:
        raise ConfigError("ANTHROPIC_API_KEY is required for draft-weekly")

    since = now.astimezone(UTC) - timedelta(days=7)
    with httpx.Client(timeout=30) as client:
        github = GitHubClient(
            settings.github_owner, settings.github_token, client=client
        )
        notifier = Notifier(
            TelegramApi(settings.telegram_bot_token, client=client),
            settings.telegram_chat_id,
        )
        if series is not None:
            name = series.repo
            meta = github.get_repo(name)
            if meta is None:
                raise GitHubError("Active series repository was not found.")
            commits = github.recent_commits(name, since, allow_empty_repo=True)
            trace(
                "draft-weekly", "source", mode="series", repo=name, commits=len(commits)
            )
        else:
            active = github.list_active_repos(since, public_only=True)
            posted = sorted(series_store.posted_repos())
            chosen = pick_project(active, posted, None)
            trace(
                "draft-weekly",
                "pick",
                active=[project.meta.name for project in active],
                posted=len(posted),
                chosen=chosen,
            )
            if chosen is None:
                excluded = sorted(project.meta.name for project in active)
                weekly.record_outcome(
                    week, "no_activity", None, {"excluded_repos": excluded}
                )
                message = (
                    "No weekly post this week: no new public project with commits."
                )
                if excluded:
                    message += f"\nActive, already posted: {', '.join(excluded)}. Use /series to continue one."
                notifier.send_alert(message)
                return
            name = chosen
            project = next(project for project in active if project.meta.name == name)
            meta, commits = project.meta, project.commits

        safety = check_repo(github, name)
        trace(
            "draft-weekly",
            "link-check",
            repo=name,
            allowed=safety.allowed,
            reasons=list(safety.reasons),
        )
        url = f"https://github.com/{settings.github_owner}/{name}"
        source = f"Source: {name}"
        if series is not None:
            source += f" · part {series.part}"
            if series.total_parts is not None:
                source += f"/{series.total_parts}"
        payload: dict[str, object]
        try:
            if planned is not None:
                assert series is not None
                prompt = "planned"
                source += " · planned text"
                payload = {
                    "repo": name,
                    "part": series.part,
                    "total_parts": series.total_parts,
                }
                de, ua = _finalize_weekly_texts(planned, url, safety.allowed)
                result: tuple[str, str] | str = (de, ua)
            else:
                prompt = "series" if series else "post"
                repo_facts: dict[str, object] = {
                    "name": name,
                    "description": meta.description,
                    "language": meta.language,
                    "visibility": meta.visibility,
                    "readme_excerpt": github.get_readme_excerpt(name, 1500),
                }
                if safety.allowed:
                    repo_facts["url"] = url
                if series is not None:
                    system = SERIES_SYSTEM_PROMPT
                    payload = {
                        "repo": repo_facts,
                        "part": series.part,
                        "total_parts": series.total_parts,
                        "recent_commits": commits[:PROMPT_COMMIT_LIMIT],
                        "commit_count": len(commits),
                        "previous_parts": [
                            {
                                "part": part.part,
                                "post_de": part.text_de,
                                "post_uk": part.text_ua,
                            }
                            for part in series_store.history(series)
                        ],
                    }
                else:
                    system = POST_SYSTEM_PROMPT
                    payload = {
                        "projects": [
                            {
                                **repo_facts,
                                "commits": commits[:PROMPT_COMMIT_LIMIT],
                                "commit_count": len(commits),
                            }
                        ],
                        "week": week.isoformat(),
                        "recently_posted": posted,
                    }

                assert api_key
                model = ClaudeClient(api_key)
                result = _generate_weekly(
                    model, system, payload, name, url, safety.allowed
                )
        except _WeeklyGenerationError as error:
            trace("draft-weekly", "generation-failed", step=str(error))
            raise RuntimeError(f"Weekly draft failed: {error}") from None
        facts: dict[str, object] = {
            "payload": payload,
            "prompt": prompt,
            "repo_url": url,
            "link_allowed": safety.allowed,
            "link_reasons": safety.reasons,
        }
        if isinstance(result, str):
            reason = result
            trace("draft-weekly", "model-skip", repo=name, reason_chars=len(reason))
            message = f"No weekly post this week ({name}): {reason}"
            with conn.transaction():
                weekly.record_outcome(
                    week, "skipped", name, {**facts, "reason": reason}
                )
                if series is not None and series_store.complete(series_id=series.id):
                    message += f"\nThe series about {name} ended."
                    if (following := series_store.get_active()) is not None:
                        message += f"\nNext series started: {following.repo}."
            notifier.send_alert(message)
            return

        link_status = (
            "link allowed"
            if safety.allowed
            else f"link blocked: {', '.join(safety.reasons)}"
        )
        source_details = f"{source} · {len(commits)} commits · {link_status}"
        facts["source_details"] = source_details
        repo = ItemRepo(conn)
        with conn.transaction():
            item = weekly.create_drafted(
                week,
                name,
                series.id if series else None,
                series.part if series else None,
                facts,
                *result,
            )
            weekly.expire_older(item.id)
            _weekly_links(settings, conn, repo, item, name, url, safety.allowed)
        trace(
            "draft-weekly",
            "drafted",
            item=item.id,
            prompt=prompt,
            **{
                f"{variant.lang}_chars": len(variant.text)
                for variant in repo.get_variants(item.id)
            },
        )
        notifier.send_preview(
            item,
            repo.get_variants(item.id),
            source_details=source_details,
        )
        trace("draft-weekly", "preview-sent", item=item.id)
        print(f"draft-weekly: drafted {name} for week {week}")


def _record_weekly_publications(conn: Connection[TupleRow], repo: ItemRepo) -> None:
    weekly = WeeklyStore(conn)
    series = SeriesStore(conn)
    unrecorded = weekly.list_unrecorded_published()
    trace("record-weekly", "pending", count=len(unrecorded))
    for pending in unrecorded:
        with conn.transaction():
            row = weekly.get(pending.week, lock=True)
            if (
                row is None
                or row.recorded_at is not None
                or row.item_id != pending.item_id
            ):
                continue
            assert row.item_id is not None and row.repo is not None
            if row.series_id is not None:
                assert row.part is not None
                texts = {
                    variant.lang: variant.text
                    for variant in repo.get_variants(row.item_id)
                }
                updated = series.record_part(
                    row.repo,
                    row.part,
                    texts["de"],
                    texts["ua"],
                    series_id=row.series_id,
                )
                following = series.get_active()
                trace(
                    "record-weekly",
                    "series-part",
                    week=row.week,
                    item=row.item_id,
                    repo=row.repo,
                    part=row.part,
                    recorded=updated is not None,
                    status=updated.status if updated else None,
                    next_part=updated.part if updated else None,
                    active=following.repo if following else None,
                    active_part=following.part if following else None,
                )
            else:
                series.mark_posted(row.repo)
                trace("record-weekly", "posted-repo", week=row.week, repo=row.repo)
            weekly.mark_recorded(row.week)


def publish_due(settings: Settings, conn: Connection[TupleRow]) -> None:
    repo = ItemRepo(conn)
    try:
        with httpx.Client() as client:
            publisher = _publisher(settings, repo, client)
            if publisher is not None:
                _publish_items(
                    publisher,
                    repo.list_unfinished_item_ids() + repo.list_approved_item_ids(),
                    notifier=Notifier(
                        TelegramApi(settings.telegram_bot_token, client=client),
                        settings.telegram_chat_id,
                    ),
                )
    finally:
        _record_weekly_publications(conn, repo)


def youtube_import_token(settings: Settings, conn: Connection[TupleRow]) -> None:
    token = os.getenv("YOUTUBE_REFRESH_TOKEN")
    if not token or not token.strip():
        raise ValueError("YOUTUBE_REFRESH_TOKEN is required")
    TokenStore(conn, TokenCipher(settings.token_encryption_key)).save(
        "youtube", "", REFRESH_TOKEN_STORAGE_EXPIRY, token
    )


def tiktok_import_token(settings: Settings, conn: Connection[TupleRow]) -> None:
    config = settings.tiktok
    if config is None or not config.refresh_token:
        raise ConfigError(
            "TikTok client credentials and refresh token are required for TIKTOK_ENV"
        )
    store = TokenStore(conn, TokenCipher(settings.token_encryption_key))
    with httpx.Client() as client:
        grant = TikTokTokenManager(config, store, client, _now).import_token(
            config.refresh_token
        )
    print(
        f"open_id={grant.open_id}, scope={grant.scope}, "
        f"refresh expiry={grant.refresh_expires_at.date().isoformat()}"
    )


def tiktok_sandbox_check(
    settings: Settings, conn: Connection[TupleRow], *, path: Path
) -> None:
    config = settings.tiktok
    if config is None or config.environment != "sandbox":
        raise ConfigError(
            "TikTok sandbox credentials and TIKTOK_ENV=sandbox are required"
        )
    store = TokenStore(conn, TokenCipher(settings.token_encryption_key))
    with path.open("rb") as video, httpx.Client() as client:
        plan = UploadPlan(os.fstat(video.fileno()).st_size)
        access = TikTokTokenManager(config, store, client, _now).access_token()
        api = TikTokClient(client, access)
        upload = api.init_inbox(plan)
        api.upload(upload, plan, video)
        status = api.status(upload.publish_id)
        for _ in range(9):
            if status.finished:
                break
            time.sleep(6)
            status = api.status(upload.publish_id)
    print(
        f"publish_id={upload.publish_id}, status={status.status}, "
        f"fail_reason={status.fail_reason or '-'}"
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
    succeeded = failed = False
    with httpx.Client() as client:
        github = GitHubClient(
            settings.github_owner, settings.github_token, client=client
        )
        for repo in sorted(repos):
            try:
                traffic = github.traffic(repo, day=day)
            except (GitHubError, Error) as error:
                failed = True
                trace(
                    "snapshot-traffic", "repo-failed", repo=repo, **error_fields(error)
                )
                logger.warning("Traffic snapshot failed for %s", repo)
                continue
            if traffic is None:
                # Private, archived, deleted or outside the token's scope: not an error.
                trace("snapshot-traffic", "repo-not-accessible", repo=repo)
                continue
            try:
                store.upsert_traffic(repo, day, traffic)
            except Error as error:
                failed = True
                trace(
                    "snapshot-traffic", "repo-failed", repo=repo, **error_fields(error)
                )
                logger.warning("Traffic snapshot failed for %s", repo)
                continue
            succeeded = True
            trace("snapshot-traffic", "repo-saved", repo=repo)

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
            failed = True
            logger.warning("Click snapshot failed for %s", link.link_id)
    finally:
        if shortio is not None:
            shortio.close()

    trace(
        "snapshot-traffic",
        "summary",
        repos=len(repos),
        links=len(links),
        saved=succeeded,
    )
    if failed and not succeeded:
        raise RuntimeError("All traffic and click snapshots failed")
