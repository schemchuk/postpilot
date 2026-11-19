from base64 import b64decode
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
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
        self, path: str, *, params: dict[str, str | int] | None = None
    ) -> httpx.Response | None:
        try:
            response = self._client.get(
                _API + path,
                headers=self._headers,
                params=params,
                follow_redirects=False,
            )
        except httpx.RequestError:
            raise GitHubError("GitHub request failed.") from None
        if response.status_code == 404:
            return None
        if not response.is_success:
            raise GitHubError(f"GitHub returned HTTP {response.status_code}.")
        return response

    def _repo_path(self, name: str) -> str:
        return f"/repos/{quote(self._owner, safe='')}/{quote(name, safe='')}"

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
