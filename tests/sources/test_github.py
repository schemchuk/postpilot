from base64 import b64encode
from datetime import UTC, date, datetime
from uuid import uuid4

import httpx
import pytest

from postpilot.sources.github import (
    ActiveRepo,
    GitHubClient,
    GitHubError,
    RepoMeta,
    pick_project,
)
from postpilot.sources.series import Series

SINCE = datetime(2026, 11, 12, tzinfo=UTC)


def repo_data(name, pushed_at="2026-11-13T00:00:00Z"):
    return {
        "name": name,
        "html_url": f"https://github.com/owner/{name}",
        "description": "A project",
        "language": "Python",
        "visibility": "public",
        "owner": {"login": "Owner"},
        "pushed_at": pushed_at,
    }


def test_list_active_repos_pages_filters_and_caps_commits():
    def respond(request):
        if request.url.path == "/users/owner/repos":
            assert request.url.params["sort"] == "pushed"
            if request.url.params["page"] == "1":
                return httpx.Response(
                    200,
                    json=[repo_data("first")],
                    headers={
                        "Link": '<https://api.github.com/users/owner/repos?page=2>; rel="next"'
                    },
                )
            assert request.url.params["page"] == "2"
            return httpx.Response(
                200,
                json=[repo_data("old", "2026-11-11T00:00:00Z"), repo_data("second")],
            )
        assert request.url.path in {
            "/repos/owner/first/commits",
            "/repos/owner/second/commits",
        }
        assert request.url.params["since"] == SINCE.isoformat()
        assert request.url.params["per_page"] == "20"
        return httpx.Response(
            200, json=[{"commit": {"message": str(i)}} for i in range(22)]
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        active = GitHubClient("owner", client=client).list_active_repos(SINCE)
    assert [repo.meta.name for repo in active] == ["first", "second"]
    assert active[0].commits == tuple(map(str, range(20)))


def test_authenticated_listing_uses_owner_affiliation(caplog):
    token = "fixture-only"
    caplog.set_level("DEBUG")

    def respond(request):
        assert request.headers["Authorization"] == f"Bearer {token}"
        assert request.url.path == "/user/repos"
        assert request.url.params["affiliation"] == "owner"
        return httpx.Response(200, json=[])

    api = GitHubClient("owner", token, transport=httpx.MockTransport(respond))
    try:
        assert api.list_active_repos(SINCE) == []
        assert token not in repr(api)
        assert token not in caplog.text
    finally:
        api.close()


def test_metadata_and_readme():
    def respond(request):
        if request.url.path == "/repos/owner/project":
            return httpx.Response(200, json=repo_data("project"))
        if request.url.path == "/repos/owner/project/readme":
            encoded = b64encode("Привіт!".encode()).decode()
            return httpx.Response(
                200,
                json={
                    "content": encoded[:4] + "\n" + encoded[4:],
                    "encoding": "base64",
                },
            )
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        api = GitHubClient("owner", client=client)
        assert api.get_repo("project") == RepoMeta(
            "project",
            "A project",
            "https://github.com/owner/project",
            "public",
            "Python",
        )
        assert api.get_readme_excerpt("project", 3) == "При"
        assert api.has_readme("project")
        assert not api.has_license("project")
        assert api.get_repo("missing") is None
        assert api.get_readme_excerpt("missing", 3) is None


@pytest.mark.parametrize("status", [403, 429, 503])
def test_http_errors_are_not_missing_files(status):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status))
    ) as client:
        api = GitHubClient("owner", client=client)
        for check in (api.has_readme, api.has_license):
            with pytest.raises(GitHubError, match=str(status)):
                check("project")


def active_repo(name, count):
    return ActiveRepo(RepoMeta(name, None, "", "public", None), ("commit",) * count)


def test_pick_project_series_wins_without_recent_activity():
    series = Series(uuid4(), "series", 2, None, date(2026, 11, 12), "active")
    assert pick_project([active_repo("other", 20)], {"series"}, series) == "series"


def test_pick_project_ranks_unposted_repos_and_breaks_ties_by_name():
    active = [
        active_repo("top", 5),
        active_repo("z", 3),
        active_repo("a", 3),
        active_repo("aa", 2),
    ]
    assert pick_project(active, {"TOP"}, None) == "a"


def test_pick_project_returns_none_when_no_repo_is_eligible():
    assert pick_project([active_repo("a", 1)], {"a"}, None) is None
    assert pick_project([], set(), None) is None
