import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from postpilot.sources.github import GitHubClient, GitHubError

_GITHUB_URL = re.compile(
    r"""(?<![\w.@/-])(?:(?:https?:)?//(?:[^\s/@<>"\[\]()]+@)?)?(?:www\.)?github\.com(?::\d+)?/[^\s<>"'`\[\]()]+""",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SafetyResult:
    allowed: bool
    reasons: tuple[str, ...]


def check_repo(
    client: GitHubClient, name: str, *, auto_fix_license: bool = False
) -> SafetyResult:
    if auto_fix_license:
        raise NotImplementedError("reviewed in a later task")
    try:
        meta = client.get_repo(name)
        if meta is None:
            return SafetyResult(False, ("repo_not_found",))
        if meta.visibility != "public":
            return SafetyResult(False, ("not_public",))
        reasons = []
        if not client.has_readme(name):
            reasons.append("missing_readme")
        if not client.has_license(name):
            reasons.append("missing_license")
    except GitHubError:
        return SafetyResult(False, ("check_failed",))
    return SafetyResult(not reasons, tuple(reasons))


def _repo_path(url: str) -> tuple[str, ...]:
    if "://" not in url and not url.startswith("//"):
        url = "//" + url
    path = unquote(urlsplit(url).path).casefold().strip("/")
    return tuple(path.split("/")[:2])


def finalize_links(text: str, repo_url: str, allowed: bool) -> str:
    if allowed:
        return text
    target = _repo_path(repo_url)

    def remove_target(match: re.Match[str]) -> str:
        url = match[0]
        path = _repo_path(url)
        if len(path) == 2:
            owner, repo = path
            if (
                path == target
                or (owner, repo.rstrip(".,;:!?").removesuffix(".git")) == target
            ):
                return ""
        return url

    return _GITHUB_URL.sub(remove_target, text)
