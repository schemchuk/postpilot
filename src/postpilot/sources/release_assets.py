import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Collection
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

import httpx

from postpilot.core.limits import LINKEDIN_TEXT_LIMIT
from postpilot.store.project_items import Asset
from postpilot.video.label import label_video_thread

_OFFSET = re.compile(r"(?:Z|[+-](?:[01][0-9]|2[0-3])(?::?[0-5][0-9])?)$")
_LIMITS = {
    "manifest.json": 16_000,
    "video.mp4": 100_000_000,
    "image.png": 8_000_000,
    "image.jpg": 8_000_000,
    "cover.png": 10_000_000,
    "post_de.txt": 4 * LINKEDIN_TEXT_LIMIT,
    "post_threads.txt": 4 * 500,
    "title_de.txt": 16_000,
}


class Rejected(ValueError):
    pass


class DownloadFailed(Rejected):
    pass


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
    except httpx.HTTPError:
        raise DownloadFailed(f"could not download asset {name}") from None
    except httpx.InvalidURL:
        raise Rejected(f"could not download asset {name}") from None
    raise Rejected(f"too many download redirects for {name}")


def _text(data: bytes, name: str, limit: int, *, label_length: int = 0) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Rejected(f"{name} must be UTF-8") from None
    if not text.strip() or "\0" in text:
        raise Rejected(f"{name} must be non-empty text without NUL")
    if len(text) + label_length > limit:
        with_label = " including the AI image label" if label_length else ""
        raise Rejected(f"{name} exceeds {limit} characters{with_label}")
    return text


def _asset_hash(data: bytes) -> Asset:
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def _threads_text(
    post_de: str,
    names: Collection[str],
    download: Callable[[str], bytes],
    *,
    label_length: int = 0,
    video_label: bool = False,
) -> str | None:
    name = "post_threads.txt"
    if name in names:
        text = _text(download(name), name, 500, label_length=label_length)
        if video_label and len(label_video_thread(text)) > 500:
            raise Rejected(
                f"{name} exceeds 500 characters including the AI video label"
            )
        return text
    fallback = label_video_thread(post_de) if video_label else post_de
    return post_de if len(fallback) + label_length <= 500 else None


def _probe_duration(video: bytes) -> float:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "video.mp4"
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


class ProjectRelease:
    def __init__(
        self, client: httpx.Client, repo: str, tag: str, *, assets: object = None
    ) -> None:
        self.client = client
        self.repo = repo
        self.tag = tag
        try:
            if assets is None:
                response = client.get(
                    f"https://api.github.com/repos/{repo}/releases/tags/{quote(tag, safe='')}",
                    follow_redirects=False,
                )
                response.raise_for_status()
                assets = response.json()["assets"]
            if not isinstance(assets, list):
                raise TypeError
            self.metadata: dict[str, dict[str, object]] = {}
            for asset in assets:
                if not isinstance(asset, dict) or not isinstance(
                    name := asset.get("name"), str
                ):
                    raise TypeError
                if name in self.metadata:
                    raise Rejected("release contains duplicate asset names")
                self.metadata[name] = asset
        except Rejected:
            raise
        except httpx.HTTPError:
            raise DownloadFailed(f"could not read release {tag}") from None
        except (ValueError, KeyError, TypeError):
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
