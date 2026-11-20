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
    RepoTraffic,
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
        assert request.url.params["per_page"] == "100"
        return httpx.Response(
            200, json=[{"commit": {"message": str(i)}} for i in range(22)]
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        active = GitHubClient("owner", client=client).list_active_repos(SINCE)
    assert [repo.meta.name for repo in active] == ["first", "second"]
    assert active[0].commits == tuple(map(str, range(22)))


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
            owner="Owner",
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


def test_traffic_uses_today_or_latest_daily_counts():
    def respond(request):
        assert request.headers["Authorization"] == "Bearer mock-traffic-token"
        path = request.url.path.removeprefix("/repos/Owner/repo/traffic/")
        return httpx.Response(
            200,
            json={
                "views": {
                    "count": 200,
                    "views": [
                        {"timestamp": "2026-11-19T00:00:00Z", "count": 7, "uniques": 4},
                        {"timestamp": "2026-11-20T00:00:00Z", "count": 2, "uniques": 1},
                    ],
                },
                "clones": {
                    "clones": [
                        {"timestamp": "2026-11-18T00:00:00Z", "count": 3, "uniques": 2},
                        {"timestamp": "2026-11-17T00:00:00Z", "count": 1, "uniques": 1},
                    ],
                },
                "popular/referrers": [{"referrer": "github.com", "count": 9}],
            }[path],
        )

    client = GitHubClient(
        "Owner", "mock-traffic-token", transport=httpx.MockTransport(respond)
    )
    try:
        assert client.traffic("repo", day=date(2026, 11, 19)) == RepoTraffic(
            7, 4, 3, 2, {"github.com": 9}
        )
    finally:
        client.close()


@pytest.mark.parametrize("status", [403, 404, 500, 200, None])
def test_traffic_errors_redact_response_and_credentials(status, caplog):
    token = "mock-private-traffic-token"

    def respond(request):
        if status is None:
            raise httpx.ConnectError(token, request=request)
        return httpx.Response(status, json={"message": token})

    client = GitHubClient("Owner", token, transport=httpx.MockTransport(respond))
    try:
        if status in (403, 404):
            assert client.traffic("repo") is None
        else:
            with pytest.raises(GitHubError) as error:
                client.traffic("repo")
            assert token not in str(error.value)
        assert token not in caplog.text
    finally:
        client.close()


def test_list_active_public_repos_with_actions_token():
    def respond(request):
        assert request.headers["Authorization"] == "Bearer fixture"
        if request.url.path == "/users/owner/repos":
            assert request.url.params["type"] == "owner"
            return httpx.Response(
                200,
                json=[
                    repo_data("eligible"),
                    {**repo_data("archived"), "archived": True},
                    {**repo_data("fork"), "fork": True},
                    {**repo_data("private"), "visibility": "private"},
                    repo_data("no-commits"),
                ],
            )
        if request.url.path == "/repos/owner/no-commits/commits":
            return httpx.Response(200, json=[])
        assert request.url.path == "/repos/owner/eligible/commits"
        return httpx.Response(200, json=[{"commit": {"message": "Change"}}])

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = GitHubClient("owner", "fixture", client=client).list_active_repos(
            SINCE, public_only=True
        )
    assert [project.meta.name for project in result] == ["eligible"]
