import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterator
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from postpilot.channels.paused import is_paused
from postpilot.channels.youtube import fitting_title
from postpilot.notify.telegram import Notifier, TelegramError
from postpilot.projects import CHANNEL_NAMES, Project, Projects
from postpilot.sources.factory import (
    _WEEKDAYS,
    DownloadFailed,
    ProjectRelease,
    Rejected,
    _payload,
    _probe_duration,
    _release_time,
    read_reel,
)
from postpilot.store.project_items import IntakeStatus, ProjectItem, ProjectItemStore

logger = logging.getLogger(__name__)
_REQUIRED = {"v", "kind", "id", "release_at", "ai"}
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _channels(value: object, project: Project) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise Rejected("channels must be a non-empty list")
    names: set[str] = set()
    for name in value:
        if isinstance(name, str) and (
            is_paused(name) or any(map(is_paused, CHANNEL_NAMES.get(name, ())))
        ):
            raise Rejected(f"channel {name} is paused")
        if not isinstance(name, str) or name not in CHANNEL_NAMES:
            raise Rejected("channels contains an unknown channel")
        if name not in project.channels:
            raise Rejected(f"channel {name} is outside the project's allowlist")
        if name in names:
            raise Rejected(f"duplicate channel {name}")
        names.add(name)
    return tuple(sorted(names))


def read_manifest(
    release: ProjectRelease,
    project: Project,
    now: datetime,
    fingerprint: str,
    probe: Callable[[bytes], float] = _probe_duration,
) -> tuple[ProjectItem, bytes]:
    raw = release.download("manifest.json", release.asset_url("manifest.json"))
    try:
        data = _payload(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise Rejected("manifest.json must be UTF-8") from None
    except Rejected as error:
        raise Rejected(str(error).replace("payload", "manifest.json")) from None

    missing = _REQUIRED - data.keys()
    unknown = data.keys() - _REQUIRED - {"channels"}
    if missing or unknown:
        raise Rejected(
            f"manifest.json keys are invalid (missing: {sorted(missing)}, unknown: {sorted(unknown)})"
        )
    if type(data["v"]) is not int or data["v"] != 2:
        raise Rejected("v must be the integer 2")
    prefix, _, suffix = release.tag.partition("/")
    if data["kind"] != prefix:
        raise Rejected("kind must match the release tag prefix")
    if prefix != "reel" or prefix not in project.kinds:
        raise Rejected(f"kind {prefix} is not allowed for project {project.key}")
    item_id = data["id"]
    if (
        not isinstance(item_id, str)
        or len(item_id) > 80
        or not _SLUG.fullmatch(item_id)
    ):
        raise Rejected("id must be a lowercase slug of at most 80 characters")
    if item_id != suffix:
        raise Rejected("id must match the release tag suffix")
    ai = data["ai"]
    if not isinstance(ai, bool):
        raise Rejected("ai must be a boolean")
    release_at = _release_time(data["release_at"], now)
    channels = _channels(data["channels"], project) if "channels" in data else None
    return read_reel(
        release,
        item_id,
        release_at,
        ai,
        probe,
        project=project.key,
        approval=project.approval,
        channels=channels,
        fingerprint=fingerprint,
    )


def _fingerprint(release: ProjectRelease) -> str:
    metadata = [
        (name, asset.get("id"), asset.get("size"), asset.get("updated_at"))
        for name, asset in sorted(release.metadata.items())
    ]
    return hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()


def _releases(
    project: Project, client: httpx.Client, token: str | None
) -> Iterator[ProjectRelease]:
    response = client.get(
        f"https://api.github.com/repos/{project.repo}/releases?per_page=30",
        headers={"Authorization": f"Bearer {token}"} if token else None,
        follow_redirects=False,
    )
    response.raise_for_status()
    releases = response.json()
    if not isinstance(releases, list):
        raise TypeError("expected a list of releases")
    for data in releases:
        if not isinstance(data, dict) or not isinstance(
            tag := data.get("tag_name"), str
        ):
            logger.warning("Project %s has invalid release metadata", project.key)
            continue
        if data.get("draft") or data.get("prerelease"):
            continue
        prefix = tag.partition("/")[0]
        if prefix != "reel" or prefix not in project.kinds:
            logger.info("Project %s ignored tag %s", project.key, tag)
            continue
        assets = data.get("assets")
        if not isinstance(assets, list):
            logger.warning("Project %s %s has invalid asset metadata", project.key, tag)
            continue
        try:
            release = ProjectRelease(client, project.repo, tag, assets=assets)
        except Rejected:
            logger.warning("Project %s %s has invalid asset metadata", project.key, tag)
            continue
        if "manifest.json" in release.metadata:
            yield release


def _notice(project: Project, reel: ProjectItem, status: IntakeStatus) -> str:
    local = reel.release_at.astimezone(ZoneInfo("Europe/Berlin"))
    channels = reel.channels if reel.channels is not None else project.channels
    lines = [
        (
            f"Project {project.key} {reel.tag} · scheduled {_WEEKDAYS[local.weekday()]} "
            f"{local:%d.%m %H:%M} Berlin · {status}"
        ),
        f"Channels: {', '.join(channels)} · AI label: {'yes' if reel.ai else 'no'}",
    ]
    if "threads" not in channels:
        lines.append("Threads skipped: channel outside this reel's targets.")
    elif reel.post_threads is None:
        lines.append(
            f"Threads skipped: post_de.txt is {len(reel.post_de)} chars (max 500), no post_threads.txt"
        )
    if "youtube" in channels:
        title = fitting_title(reel.post_de, reel.title_de)
        lines.append(
            f"YouTube title: {title}"
            if title
            else "YouTube will use the default title."
        )
    else:
        lines.append("YouTube title: channel omitted.")
    lines.append(f"https://github.com/{project.repo}/releases/tag/{quote(reel.tag)}")
    return "\n".join(lines)


def _intake(
    release: ProjectRelease,
    project: Project,
    projects: Projects,
    store: ProjectItemStore,
    notify: Notifier,
    now: datetime,
) -> None:
    fingerprint = _fingerprint(release)
    if store.seen(project.key, release.tag, fingerprint):
        return
    try:
        reel, cover = read_manifest(release, project, now, fingerprint)
        with store.conn.transaction():
            status, reason = store.save_reel(projects, reel)
            if status == "refused":
                raise Rejected(f"replacement refused: {reason}")
            if project.approval == "auto":
                notify.send_alert(_notice(project, reel, status))
            elif status in {"new", "replaced"}:
                item_id = store.preview_item_id(reel)
                message_id = notify.send_project_preview(
                    item_id,
                    cover,
                    _notice(project, reel, status),
                    reel.post_de,
                    reel.post_threads
                    if "threads" in (reel.channels or project.channels)
                    else None,
                )
                store.set_preview_message(item_id, message_id)
    except DownloadFailed:
        raise
    except Rejected as error:
        with store.conn.transaction():
            if store.record_rejection(
                project.key, release.tag, fingerprint, str(error)
            ):
                notify.send_alert(
                    f"Project {project.key} {release.tag} rejected: {error}"
                )


def scan_projects(
    projects: Projects,
    client: httpx.Client,
    store: ProjectItemStore,
    notify: Notifier,
    now: datetime,
    only: str | None = None,
    *,
    token: str | None = None,
) -> bool:
    ok = True
    for project in projects:
        if not project.enabled or (only is not None and project.key != only):
            continue
        try:
            for release in _releases(project, client, token):
                try:
                    _intake(release, project, projects, store, notify, now)
                except DownloadFailed as error:
                    logger.warning("Project %s %s: %s", project.key, release.tag, error)
                    ok = False
                except TelegramError:
                    logger.warning(
                        "Project %s %s Telegram notice failed", project.key, release.tag
                    )
                    ok = False
        except (httpx.HTTPError, ValueError, TypeError, RecursionError) as error:
            logger.warning(
                "Project %s scan failed (%s)", project.key, type(error).__name__
            )
            ok = False
    return ok
