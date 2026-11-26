import os
import re
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from postpilot.channels.paused import is_paused
from postpilot.video.publications import LINKEDIN_VIDEO_CHANNELS

FACTORY_PROJECT = "beekeeper"
DEFAULT_PROJECTS_FILE = Path(__file__).resolve().parents[2] / "config/projects.toml"
CHANNEL_NAMES = {
    "linkedin-de": tuple(
        name for name, lang in LINKEDIN_VIDEO_CHANNELS.items() if lang == "de"
    ),
    "threads": ("threads",),
    "instagram": ("instagram-reels",),
    "facebook": ("facebook-reels",),
    "youtube": ("youtube",),
    "tiktok": ("tiktok",),
}


class ProjectsConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Project:
    key: str
    repo: str
    kinds: tuple[str, ...]
    channels: tuple[str, ...]
    approval: str = "tap"
    max_per_day: int = 1
    enabled: bool = True


class Projects:
    def __init__(self, projects: tuple[Project, ...] = ()) -> None:
        self._projects = {project.key: project for project in projects}
        self._repos = {project.repo.lower(): project for project in projects}

    def get(self, key: str) -> Project | None:
        return self._projects.get(key)

    def by_repo(self, repo: str) -> Project | None:
        return self._repos.get(repo.lower())

    def __iter__(self) -> Iterator[Project]:
        return iter(self._projects.values())

    @classmethod
    def load(cls, path: Path | None = None) -> "Projects":
        if path is None:
            override = os.environ.get("PROJECTS_FILE")
            if override:
                path = Path(override)
            elif DEFAULT_PROJECTS_FILE.is_file():
                path = DEFAULT_PROJECTS_FILE
            else:
                # A non-editable installation runs the workflow from the checkout root.
                path = Path.cwd() / "config/projects.toml"
        try:
            with path.open("rb") as file:
                config = tomllib.load(file)
        except (OSError, ValueError) as error:
            raise ProjectsConfigError(f"{path}: {error}") from error
        entries = config.get("projects", {})
        if not isinstance(entries, dict):
            raise ProjectsConfigError("projects must be a table")
        projects: list[Project] = []
        repos: set[str] = set()
        for key, fields in entries.items():
            project = _parse_project(key, fields)
            repo = project.repo.lower()
            if repo in repos:
                raise ProjectsConfigError(f"projects.{key}.repo: duplicate repository")
            repos.add(repo)
            projects.append(project)
        return cls(tuple(projects))


def _parse_project(key: str, fields: object) -> Project:
    def error(field: str, message: str) -> ProjectsConfigError:
        return ProjectsConfigError(f"projects.{key}.{field}: {message}")

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", key):
        raise error("key", "expected a lowercase slug of at most 40 characters")
    if not isinstance(fields, dict):
        raise error("entry", "expected a table")

    def choices(field: str, allowed: set[str]) -> tuple[str, ...]:
        value = fields.get(field)
        if (
            not isinstance(value, list)
            or not value
            or any(
                not isinstance(entry, str) or entry not in allowed for entry in value
            )
        ):
            raise error(field, f"expected a non-empty list from {sorted(allowed)}")
        if len(value) != len(set(value)):
            raise error(field, "duplicate values")
        return tuple(value)

    for field in fields.keys() - {
        "repo",
        "kinds",
        "channels",
        "approval",
        "max_per_day",
        "enabled",
    }:
        raise error(field, "unknown field")
    repo = fields.get("repo")
    if (
        not isinstance(repo, str)
        or not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}", repo
        )
        or repo.partition("/")[2] in {".", ".."}
    ):
        raise error("repo", "expected a GitHub owner/name")
    kinds = choices("kinds", {"reel", "post"})
    channels = choices(
        "channels",
        {
            logical
            for logical, names in CHANNEL_NAMES.items()
            if names and not any(map(is_paused, names))
        },
    )
    approval = fields.get("approval", "tap")
    if approval not in ("auto", "tap"):
        raise error("approval", "expected auto or tap")
    max_per_day = fields.get("max_per_day", 1)
    if type(max_per_day) is not int or max_per_day < 1:
        raise error("max_per_day", "expected an integer >= 1")
    enabled = fields.get("enabled", True)
    if not isinstance(enabled, bool):
        raise error("enabled", "expected a boolean")
    return Project(key, repo, kinds, channels, approval, max_per_day, enabled)
