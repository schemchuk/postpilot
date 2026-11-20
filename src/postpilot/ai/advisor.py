from typing import Protocol

from postpilot.ai.prompts import ADVISOR_SYSTEM_PROMPT, advisor_message
from postpilot.sources.github import GitHubClient


class AdvisorModel(Protocol):
    def complete(self, system: str, user: str, *, max_tokens: int) -> str: ...


def generate_advice(
    model: AdvisorModel, github: GitHubClient, owner: str, profile: str
) -> str:
    # GitHub's pushed_at timestamps are UTC ISO 8601 strings.
    repos = sorted(
        (repo for repo in github.list_public_repos(owner) if not repo.fork),
        key=lambda repo: repo.pushed_at or "",
        reverse=True,
    )
    projects = []
    for index, repo in enumerate(repos):
        data: dict[str, object] = {
            "name": repo.name,
            "description": repo.description,
            "language": repo.language,
            "pushed_at": repo.pushed_at,
        }
        if index < 10 and (excerpt := github.get_readme_excerpt(repo.name, 2500)):
            data["readme_excerpt"] = excerpt
        projects.append(data)
    return model.complete(
        ADVISOR_SYSTEM_PROMPT, advisor_message(profile, projects), max_tokens=8000
    )
