import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from postpilot.channels.youtube import fitting_title
from postpilot.core.limits import (
    LINKEDIN_TEXT_LIMIT,
    REELS_MAX_DURATION,
    REELS_MIN_DURATION,
)
from postpilot.projects import FACTORY_PROJECT
from postpilot.store.project_items import Asset, IntakeStatus, ProjectItem
from postpilot.video.label import AI_VIDEO_CAPTION_LABEL

_EPISODE = re.compile(r"[0-9]{3}-[a-z0-9]+(?:-[a-z0-9]+)*")
_OFFSET = re.compile(r"(?:Z|[+-](?:[01][0-9]|2[0-3])(?::?[0-5][0-9])?)$")
_LIMITS = {
    "reel_de.mp4": 100_000_000,
    "cover_de.png": 10_000_000,
    "post_de.txt": 4 * LINKEDIN_TEXT_LIMIT,
    "post_threads.txt": 4 * 500,
    "title_de.txt": 16_000,
}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class Rejected(ValueError):
    pass


def _episode(value: object) -> str:
    if isinstance(value, str) and len(value) <= 80 and _EPISODE.fullmatch(value):
        return value
    return "?"


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise Rejected("payload contains duplicate keys")
    return result


def _payload(raw: str) -> dict[str, object]:
    try:
        data = json.loads(raw, object_pairs_hook=_unique_keys)
    except Rejected:
        raise
    except (ValueError, RecursionError):
        raise Rejected("payload must be valid JSON") from None
    if not isinstance(data, dict):
        raise Rejected("payload must be a JSON object")
    return data


def _release_time(value: object, now: datetime) -> datetime:
    try:
        if not isinstance(value, str) or not _OFFSET.search(value.partition("T")[2]):
            raise ValueError
        parsed = datetime.fromisoformat(value)
        release_at = parsed.astimezone(UTC) if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        release_at = None
    if release_at is None:
        raise Rejected("release_at must be ISO 8601 with an explicit offset")
    if not now - timedelta(hours=1) <= release_at <= now + timedelta(days=60):
        raise Rejected("release_at must be within 1 hour in the past and 60 days ahead")
    return release_at


def _asset_url(url: httpx.URL, repo: str, *, redirect: bool = False) -> bool:
    if url.scheme != "https" or url.userinfo or url.port not in {None, 443}:
        return False
    if url.host == "objects.githubusercontent.com":
        return True
    # GitHub serves redirected release downloads from its release asset CDN.
    if redirect and url.host == "release-assets.githubusercontent.com":
        return True
    return (
        url.host == "github.com"
        and url.path.startswith(f"/{repo}/releases/download/")
        and not {".", ".."}.intersection(url.path.split("/"))
        and "\\" not in url.path
    )


def _asset(client: httpx.Client, url: httpx.URL, repo: str, name: str) -> bytes:
    limit = _LIMITS[name]
    data = bytearray()
    try:
        for _ in range(6):
            with client.stream("GET", url, follow_redirects=False) as response:
                if response.next_request is not None:
                    url = response.next_request.url
                    if not _asset_url(url, repo, redirect=True):
                        raise Rejected(f"{name} has a disallowed download redirect")
                    continue
                response.raise_for_status()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if len(data) + len(chunk) > limit:
                        raise Rejected(f"{name} exceeds {limit} bytes")
                    data.extend(chunk)
                return bytes(data)
    except (httpx.HTTPError, httpx.InvalidURL):
        raise Rejected(f"could not download asset {name}") from None
    raise Rejected(f"too many download redirects for {name}")


def _text(data: bytes, name: str, limit: int) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Rejected(f"{name} must be UTF-8") from None
    if not text.strip() or "\0" in text:
        raise Rejected(f"{name} must be non-empty text without NUL")
    if len(text) > limit:
        raise Rejected(f"{name} exceeds {limit} characters")
    return text


def _probe_duration(video: bytes) -> float:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "reel_de.mp4"
        path.write_bytes(video)
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-f",
                "mov",
                "-select_streams",
                "V:0",
                "-show_entries",
                "stream=index:format=duration",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        media = json.loads(result.stdout)
        if not media["streams"]:
            raise ValueError("No video stream")
        return float(media["format"]["duration"])


class FactoryRelease:
    def __init__(self, client: httpx.Client, repo: str, tag: str) -> None:
        self.client = client
        self.repo = repo
        self.tag = tag
        try:
            response = client.get(
                f"https://api.github.com/repos/{repo}/releases/tags/{quote(tag, safe='')}",
                follow_redirects=False,
            )
            response.raise_for_status()
            self.metadata = {
                asset["name"]: asset for asset in response.json()["assets"]
            }
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise Rejected(f"could not read release {tag}") from None

    def asset_url(self, name: str) -> httpx.URL:
        if name not in self.metadata:
            raise Rejected(f"release {self.tag} is missing {name}")
        size = self.metadata[name].get("size")
        limit = _LIMITS[name]
        if type(size) is not int or size < 0:
            raise Rejected(f"{name} has an invalid release asset size")
        if size > limit:
            raise Rejected(f"{name} exceeds {limit} bytes")
        raw_url = self.metadata[name].get("browser_download_url")
        try:
            url = httpx.URL(raw_url) if isinstance(raw_url, str) else httpx.URL()
        except httpx.InvalidURL:
            raise Rejected(f"{name} has a disallowed download URL") from None
        if not _asset_url(url, self.repo):
            raise Rejected(f"{name} has a disallowed download URL")
        return url

    def download(self, name: str, url: httpx.URL) -> bytes:
        return _asset(self.client, url, self.repo, name)


def _download(
    client: httpx.Client,
    repo: str,
    episode: str,
    release_at: datetime,
    ai: bool,
    probe: Callable[[bytes], float],
) -> ProjectItem:
    release = FactoryRelease(client, repo, f"reel/{episode}")
    urls = {
        name: release.asset_url(name)
        for name in _LIMITS
        if name != "title_de.txt"
        and (name != "post_threads.txt" or name in release.metadata)
    }
    assets: dict[str, Asset] = {}

    def download(name: str) -> bytes:
        data = release.download(name, urls[name])
        assets[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        return data

    post_de = _text(download("post_de.txt"), "post_de.txt", LINKEDIN_TEXT_LIMIT)
    label = f"\n\n{AI_VIDEO_CAPTION_LABEL}" if ai else ""
    if len(post_de + label) > 2200:
        raise Rejected(
            "post_de.txt exceeds the 2200-character Instagram caption limit (including any AI label)"
        )
    threads = post_de if len(post_de) <= 500 else None
    if "post_threads.txt" in urls:
        threads = _text(download("post_threads.txt"), "post_threads.txt", 500)
    if not download("cover_de.png").startswith(b"\x89PNG\r\n\x1a\n"):
        raise Rejected("cover_de.png must have a PNG signature")
    video = download("reel_de.mp4")
    try:
        duration = probe(video)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        raise Rejected("could not probe reel_de.mp4 duration") from None
    if not REELS_MIN_DURATION <= duration <= REELS_MAX_DURATION:
        raise Rejected(
            f"reel_de.mp4 must be {REELS_MIN_DURATION}-{REELS_MAX_DURATION} seconds"
        )
    title = None
    if (title_asset := release.metadata.get("title_de.txt")) is not None:
        size = title_asset.get("size")
        title = ""
        if not (type(size) is int and size > _LIMITS["title_de.txt"]):
            urls["title_de.txt"] = release.asset_url("title_de.txt")
            try:
                raw_title = download("title_de.txt").decode("utf-8")
            except UnicodeDecodeError:
                raw_title = ""
            if "\0" not in raw_title:
                title = fitting_title("", raw_title)
    return ProjectItem(
        episode,
        release_at,
        ai,
        post_de,
        threads,
        assets,
        duration,
        FACTORY_PROJECT,
        title_de=title,
    )


def _notice(repo: str, reel: ProjectItem, status: IntakeStatus) -> str:
    local = reel.release_at.astimezone(ZoneInfo("Europe/Berlin"))
    threads = "" if reel.post_threads is None else "Threads, "
    lines = [
        (
            f"Factory reel {reel.id} · scheduled {_WEEKDAYS[local.weekday()]} "
            f"{local:%d.%m %H:%M} · LinkedIn DE, {threads}IG, FB, YouTube, TikTok · AI label: "
            f"{'yes' if reel.ai else 'no'}"
        )
    ]
    if reel.post_threads is None:
        lines.append(
            f"Threads skipped: post_de.txt is {len(reel.post_de)} chars (max 500), no post_threads.txt"
        )
    if not fitting_title(reel.post_de, reel.title_de):
        lines.append("YouTube will use the default title.")
    lines.append(
        f"Status: {'refused (already published)' if status == 'refused' else status}"
    )
    lines.append(f"https://github.com/{repo}/releases/tag/{reel.tag}")
    return "\n".join(lines)


def intake(
    raw: str,
    repo: str,
    client: httpx.Client,
    save: Callable[[ProjectItem], IntakeStatus],
    notify: Callable[[str], None],
    *,
    now: datetime | None = None,
    probe: Callable[[bytes], float] = _probe_duration,
) -> int:
    episode = "?"
    try:
        data = _payload(raw)
        episode = _episode(data.get("episode"))
        if data.keys() != {"v", "episode", "release_at", "ai"}:
            raise Rejected("payload keys must be exactly v, episode, release_at, ai")
        if type(data["v"]) is not int or data["v"] != 1:
            raise Rejected("v must be the integer 1")
        if episode == "?":
            raise Rejected("episode must be a factory slug of at most 80 characters")
        ai = data["ai"]
        if not isinstance(ai, bool):
            raise Rejected("ai must be a boolean")
        release_at = _release_time(data["release_at"], now or datetime.now(UTC))
        reel = _download(client, repo, episode, release_at, ai, probe)
    except Rejected as error:
        notify(f"Factory reel {episode} rejected: {error}")
        return 1
    status = save(reel)
    notify(_notice(repo, reel, status))
    return int(status == "refused")
