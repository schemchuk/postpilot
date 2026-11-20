from datetime import UTC, datetime
from unittest.mock import Mock, call
from uuid import uuid4

import httpx
import pytest
from psycopg import Error

from postpilot.engagement.job import collect_github, notify_pending
from postpilot.engagement.store import EngagementEvent, EngagementStore
from postpilot.sources.github import (
    GitHubClient,
    GitHubError,
    PublicRepo,
    RepoEngagement,
)
from postpilot.store.bot_state import BotState

NOW = datetime(2026, 11, 20, tzinfo=UTC)
OWNER = "owner"
REPO = "repo"
URL = f"https://github.com/{OWNER}/{REPO}"
NEXT = '<https://untrusted.example/next>; rel="next"'


def test_public_repo_pagination_uses_fixed_endpoint_and_excludes_private():
    def handler(request):
        assert request.url.host == "api.github.com"
        assert request.url.path == "/users/owner/repos"
        assert request.url.params["per_page"] == "100"
        assert request.url.params["type"] == "owner"
        assert request.headers["authorization"] == "Bearer test-token"
        page = int(request.url.params["page"])
        row = {
            "id": page,
            "name": REPO,
            "html_url": URL,
            "stargazers_count": 0,
            "forks_count": 3,
            "archived": True,
        }
        return httpx.Response(
            200,
            json=[row, {**row, "private": True}],
            headers={"Link": NEXT} if page == 1 else {},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        github = GitHubClient(OWNER, "test-token", client=http)
        assert github.list_public_repos(OWNER) == [
            PublicRepo(1, REPO, URL, 0, 3),
            PublicRepo(2, REPO, URL, 0, 3),
        ]


@pytest.mark.parametrize("endpoint", ["stargazers", "forks"])
def test_watch_lists_parse_and_cap_pages(endpoint):
    pages = []

    def handler(request):
        assert request.url.path == f"/repos/owner/repo/{endpoint}"
        assert request.url.params["per_page"] == "100"
        assert "authorization" not in request.headers
        page = int(request.url.params["page"])
        pages.append(page)
        timestamp = "2026-11-20T01:00:00+01:00"
        if endpoint == "stargazers":
            assert request.headers["accept"] == "application/vnd.github.star+json"
            row = {"starred_at": timestamp, "user": {"id": page, "login": "actor"}}
        else:
            row = {"id": page, "created_at": timestamp, "owner": {"login": "actor"}}
        return httpx.Response(200, json=[row], headers={"Link": NEXT})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        entries = getattr(GitHubClient(OWNER, client=http), endpoint)(OWNER, REPO)
    assert pages == list(range(1, 11))
    assert entries == [RepoEngagement(page, "actor", NOW) for page in pages]


@pytest.mark.parametrize("status", [302, 403, 404, 200])
def test_watch_errors_are_typed_and_do_not_include_token(status):
    token = "sensitive-test-token"
    transport = httpx.MockTransport(lambda _: httpx.Response(status, text=token))
    with (
        httpx.Client(transport=transport, follow_redirects=True) as http,
        pytest.raises(GitHubError) as error,
    ):
        GitHubClient(OWNER, token, client=http).list_public_repos(OWNER)
    assert token not in str(error.value)


def test_missing_date_falls_back_to_aware_utc():
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json=[{"id": 1, "owner": {"login": "actor"}}])
    )
    with httpx.Client(transport=transport) as http:
        before = datetime.now(UTC)
        [fork] = GitHubClient(OWNER, client=http).forks(OWNER, REPO)
        assert before <= fork.occurred_at <= datetime.now(UTC)
        assert fork.occurred_at.tzinfo is UTC


@pytest.fixture
def watch():
    client = Mock(spec=GitHubClient)
    state = Mock(spec=BotState)
    store = Mock(spec=EngagementStore)
    counts = {}
    state.get_github_watch.side_effect = counts.copy
    state.set_github_watch.side_effect = counts.__setitem__
    rows = {}

    def insert(channel, kind, ref, actor, text, url, draft, occurred, *, notified):
        rows.setdefault(
            ref,
            EngagementEvent(
                uuid4(),
                channel,
                kind,
                ref,
                actor,
                text,
                url,
                draft,
                occurred,
                NOW if notified else None,
                None,
                NOW,
            ),
        )

    store.insert_if_new.side_effect = insert
    store.list_unnotified.side_effect = lambda: [
        event for event in rows.values() if event.notified_at is None
    ]
    store.known_refs.side_effect = lambda _channel, refs: rows.keys() & set(refs)
    return client, state, store, counts


def test_baseline_and_changes_only_notify_new_events(watch):
    client, state, store, counts = watch
    api = Mock()
    client.list_public_repos.return_value = [
        PublicRepo(1, REPO, URL, 1, 1),
        PublicRepo(2, "empty", f"{URL}-empty", 0, 0),
    ]
    client.stargazers.return_value = [RepoEngagement(10, "star-user", NOW)]
    client.forks.return_value = [RepoEngagement(20, "fork-user", NOW)]

    collect_github(client, OWNER, state, store)
    assert store.insert_if_new.call_args_list == [
        call(
            "github",
            "star",
            "star:1:10",
            "star-user",
            None,
            URL,
            None,
            NOW,
            notified=True,
        ),
        call(
            "github",
            "fork",
            "fork:1:20",
            "fork-user",
            None,
            URL,
            None,
            NOW,
            notified=True,
        ),
    ]
    assert counts == {"1": {"stars": 1, "forks": 1}, "2": {"stars": 0, "forks": 0}}
    notify_pending(store, api, 42)
    api.send_message.assert_not_called()

    collect_github(client, OWNER, state, store)
    client.stargazers.assert_called_once_with(OWNER, REPO)
    client.forks.assert_called_once_with(OWNER, REPO)

    client.list_public_repos.return_value = [PublicRepo(1, REPO, URL, 2, 1)]
    client.stargazers.return_value += [RepoEngagement(11, "new-star", NOW)]
    collect_github(client, OWNER, state, store)
    client.forks.assert_called_once_with(OWNER, REPO)

    client.list_public_repos.return_value = [PublicRepo(1, REPO, URL, 2, 2)]
    client.forks.return_value += [RepoEngagement(21, "new-fork", NOW)]
    collect_github(client, OWNER, state, store)
    assert client.stargazers.call_count == 2
    assert store.insert_if_new.call_args_list[2:] == [
        call(
            "github",
            "star",
            "star:1:11",
            "new-star",
            None,
            URL,
            None,
            NOW,
            notified=False,
        ),
        call(
            "github",
            "fork",
            "fork:1:21",
            "new-fork",
            None,
            URL,
            None,
            NOW,
            notified=False,
        ),
    ]
    assert counts["1"] == {"stars": 2, "forks": 2}


@pytest.mark.parametrize("failure", ["list", "insert"])
def test_repo_failure_keeps_counts_and_processes_later_repos(watch, failure):
    client, state, store, counts = watch
    counts["1"] = {"stars": 0, "forks": 0}
    client.list_public_repos.return_value = [
        PublicRepo(1, REPO, URL, 1, 1),
        PublicRepo(2, "next", f"{URL}-next", 0, 0),
    ]
    client.stargazers.return_value = [RepoEngagement(10, "star-user", NOW)]
    client.forks.return_value = []
    if failure == "list":
        client.forks.side_effect = GitHubError("failed")
    else:
        store.insert_if_new.side_effect = Error("failed")
    with pytest.raises(GitHubError, match="Could not process all"):
        collect_github(client, OWNER, state, store)
    assert counts == {"1": {"stars": 0, "forks": 0}, "2": {"stars": 0, "forks": 0}}
    if failure == "list":
        store.insert_if_new.assert_not_called()


@pytest.mark.parametrize(
    ("kind", "actor", "expected"),
    [
        (
            "star",
            "valid-user",
            '⭐ <a href="https://github.com/valid-user">valid-user</a> starred',
        ),
        ("fork", 'a<&"', "🍴 a&lt;&amp;&quot; forked"),
    ],
)
def test_github_notification_formats_and_escapes(kind, actor, expected):
    event = EngagementEvent(
        uuid4(),
        "github",
        kind,
        "ref",
        actor,
        None,
        "https://github.com/owner/a%3C%26",
        None,
        NOW,
        None,
        None,
        NOW,
    )
    store = Mock()
    store.list_unnotified.return_value = [event]
    api = Mock()
    api.send_message.return_value = 55
    notify_pending(store, api, 42)
    api.send_message.assert_called_once_with(
        42, f"{expected} <b>a&lt;&amp;</b>", None, parse_mode="HTML"
    )
    store.mark_notified.assert_called_once_with(event.id, 55)
