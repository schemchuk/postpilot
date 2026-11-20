from base64 import b64decode
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import count
from typing import Any
from urllib.parse import quote

import httpx

from postpilot.sources.series import Series

_API = "https://api.github.com"
_PAGE_SIZE = 100
_COMMIT_LIMIT = 20


class GitHubError(RuntimeError):
    """GitHub did not return a usable response."""


@dataclass(frozen=True)
class RepoMeta:
    name: str
    description: str | None
    url: str
    visibility: str
    language: str | None

    @property
    def is_private(self) -> bool:
        return self.visibility == "private"


@dataclass(frozen=True)
class ActiveRepo:
    meta: RepoMeta
    commits: tuple[str, ...]


@dataclass(frozen=True)
class RepoTraffic:
    views: int
    unique_views: int
    clones: int
    unique_clones: int
    referrers: dict[str, int]


@dataclass(frozen=True)
class PublicRepo:
    id: int
    name: str
    html_url: str
    stargazers_count: int
    forks_count: int
    description: str | None = None
    language: str | None = None
    pushed_at: str | None = None
    fork: bool = False
    archived: bool = False


@dataclass(frozen=True)
class RepoEngagement:
    id: int
    login: str
    occurred_at: datetime


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError
    return value


def _engagement(actor_id: object, login: object, timestamp: object) -> RepoEngagement:
    try:
        occurred_at = datetime.fromisoformat(_string(timestamp))
    except ValueError:
        occurred_at = datetime.now(UTC)
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    return RepoEngagement(_count(actor_id), _string(login), occurred_at.astimezone(UTC))


def _public_repo(row: dict[str, Any]) -> PublicRepo | None:
    if row.get("private") or row.get("visibility") in ("private", "internal"):
        return None
    return PublicRepo(
        _count(row["id"]),
        _string(row["name"]),
        _string(row["html_url"]),
        _count(row["stargazers_count"]),
        _count(row["forks_count"]),
        row.get("description"),
        row.get("language"),
        row.get("pushed_at"),
        row.get("fork", False),
        row.get("archived", False),
    )


def _daily_counts(rows: list[dict[str, Any]], day: str) -> tuple[int, int]:
    by_day = {
        date.fromisoformat(row["timestamp"][:10]).isoformat(): row for row in rows
    }
    if not by_day:
        return 0, 0
    row = by_day[day if day in by_day else max(by_day)]
    return _count(row["count"]), _count(row["uniques"])


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError
    return value


def _repo_meta(data: dict[str, Any]) -> RepoMeta:
    return RepoMeta(
        name=data["name"],
        description=data.get("description"),
        url=data["html_url"],
        visibility=data["visibility"],
        language=data.get("language"),
    )


class GitHubClient:
    def __init__(
        self,
        owner: str,
        token: str | None = None,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("Pass either a client or a transport.")
        self._owner = owner
        self._authenticated = token is not None
        self._headers = {"Accept": "application/vnd.github+json"}
        if token is not None:
            self._headers["Authorization"] = f"Bearer {token}"
        self._owns_client = client is None
        self._client = (
            client if client is not None else httpx.Client(transport=transport)
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _get(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        allow_forbidden: bool = False,
        accept: str | None = None,
    ) -> httpx.Response | None:
        try:
            response = self._client.get(
                _API + path,
                headers={**self._headers, "Accept": accept}
                if accept
                else self._headers,
                params=params,
                follow_redirects=False,
            )
        except httpx.RequestError:
            raise GitHubError("GitHub request failed.") from None
        if response.status_code == 404 or (
            allow_forbidden and response.status_code == 403
        ):
            return None
        if not response.is_success:
            raise GitHubError(f"GitHub returned HTTP {response.status_code}.")
        return response

    def _repo_path(self, name: str, *, owner: str | None = None) -> str:
        return f"/repos/{quote(owner or self._owner, safe='')}/{quote(name, safe='')}"

    def _watch_pages[T](
        self,
        path: str,
        parse: Callable[[dict[str, Any]], T | None],
        *,
        params: dict[str, str | int] | None = None,
        accept: str | None = None,
        max_pages: int | None = None,
    ) -> list[T]:
        results: list[T] = []
        try:
            for page in count(1):
                response = self._get(
                    path,
                    params={**(params or {}), "per_page": _PAGE_SIZE, "page": page},
                    accept=accept,
                )
                if response is None:
                    raise GitHubError("GitHub watch endpoint was not found.")
                rows = response.json()
                if not isinstance(rows, list):
                    raise TypeError
                for row in rows:
                    if not isinstance(row, dict):
                        raise TypeError
                    if (result := parse(row)) is not None:
                        results.append(result)
                if "next" not in response.links or page == max_pages:
                    break
        except (KeyError, TypeError, ValueError, OverflowError):
            raise GitHubError("GitHub returned invalid watch data.") from None
        return results

    def list_public_repos(self, owner: str) -> list[PublicRepo]:
        return self._watch_pages(
            f"/users/{quote(owner, safe='')}/repos",
            _public_repo,
            params={"type": "owner"},
        )

    def stargazers(self, owner: str, repo: str) -> list[RepoEngagement]:
        return self._watch_pages(
            f"{self._repo_path(repo, owner=owner)}/stargazers",
            lambda row: _engagement(
                row["user"]["id"], row["user"]["login"], row.get("starred_at")
            ),
            accept="application/vnd.github.star+json",
            max_pages=10,
        )

    def forks(self, owner: str, repo: str) -> list[RepoEngagement]:
        return self._watch_pages(
            f"{self._repo_path(repo, owner=owner)}/forks",
            lambda row: _engagement(
                row["id"], row["owner"]["login"], row.get("created_at")
            ),
            max_pages=10,
        )

    def list_active_repos(self, since: datetime) -> list[ActiveRepo]:
        if since.utcoffset() is None:
            raise ValueError("since must include a timezone.")
        since = since.astimezone(UTC)
        if self._authenticated:
            path = "/user/repos"
            params: dict[str, str | int] = {"affiliation": "owner"}
        else:
            path = f"/users/{quote(self._owner, safe='')}/repos"
            params = {"type": "owner"}
        params.update(sort="pushed", direction="desc", per_page=_PAGE_SIZE)
        active = []
        for page in count(1):
            params["page"] = page
            response = self._get(path, params=params)
            if response is None:
                raise GitHubError("GitHub repository listing was not found.")
            for repo in response.json():
                pushed_at = repo["pushed_at"]
                if not pushed_at or datetime.fromisoformat(pushed_at) < since:
                    continue
                if repo["owner"]["login"].casefold() != self._owner.casefold():
                    continue
                meta = _repo_meta(repo)
                commits = self._get(
                    self._repo_path(meta.name) + "/commits",
                    params={"since": since.isoformat(), "per_page": _COMMIT_LIMIT},
                )
                if commits is None:
                    raise GitHubError("GitHub commit listing was not found.")
                messages = tuple(
                    entry["commit"]["message"]
                    for entry in commits.json()[:_COMMIT_LIMIT]
                )
                active.append(ActiveRepo(meta, messages))
            if "next" not in response.links:
                break
        return active

    def traffic(self, name: str, *, day: date | None = None) -> RepoTraffic | None:
        """Fetch daily traffic using a token with push access to the repository."""
        if not self._authenticated:
            return None
        path = self._repo_path(name) + "/traffic/"
        today = (day or datetime.now(UTC).date()).isoformat()
        try:
            responses = []
            for endpoint in ("views", "clones", "popular/referrers"):
                response = self._get(path + endpoint, allow_forbidden=True)
                if response is None:
                    return None
                responses.append(response)
            views, clones, referrers = responses
            return RepoTraffic(
                *_daily_counts(views.json()["views"], today),
                *_daily_counts(clones.json()["clones"], today),
                {row["referrer"]: _count(row["count"]) for row in referrers.json()},
            )
        except (KeyError, TypeError, AttributeError, ValueError):
            raise GitHubError("GitHub returned invalid traffic data.") from None

    def get_repo(self, name: str) -> RepoMeta | None:
        response = self._get(self._repo_path(name))
        if response is None:
            return None
        try:
            return _repo_meta(response.json())
        except (KeyError, TypeError, ValueError):
            raise GitHubError("GitHub returned invalid repository metadata.") from None

    def get_readme_excerpt(self, name: str, max_chars: int) -> str | None:
        if max_chars < 0:
            raise ValueError("max_chars must be nonnegative.")
        response = self._get(self._repo_path(name) + "/readme")
        if response is None:
            return None
        try:
            data = response.json()
            if data["encoding"] != "base64":
                raise ValueError("Expected base64.")
            encoded = "".join(data["content"].split())
            return b64decode(encoded, validate=True).decode("utf-8", errors="replace")[
                :max_chars
            ]
        except (AttributeError, KeyError, TypeError, ValueError):
            raise GitHubError("GitHub returned invalid README content.") from None

    def has_license(self, name: str) -> bool:
        return self._get(self._repo_path(name) + "/license") is not None

    def has_readme(self, name: str) -> bool:
        return self._get(self._repo_path(name) + "/readme") is not None


def pick_project(
    active: Iterable[ActiveRepo], posted: Collection[str], series: Series | None
) -> str | None:
    if series is not None and series.status == "active":
        return series.repo
    excluded = {name.casefold() for name in posted}
    eligible = (repo for repo in active if repo.meta.name.casefold() not in excluded)
    chosen = min(
        eligible,
        key=lambda repo: (-len(repo.commits), repo.meta.name),
        default=None,
    )
    return chosen.meta.name if chosen is not None else None
