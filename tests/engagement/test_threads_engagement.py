from datetime import UTC, datetime

import httpx
import pytest

from postpilot.engagement.threads import (
    Reply,
    ThreadsEngagementClient,
    ThreadsEngagementError,
)

TOKEN = "private-token"
PROFILE = {"id": "me", "username": "owner"}
ROW = {
    "id": "reply",
    "text": "Hello",
    "username": "reader",
    "timestamp": "2026-11-20T12:00:00+00:00",
    "permalink": "https://www.threads.com/@reader/post/reply",
}


def test_conversation_parses_filters_and_paginates_with_cached_profile():
    paths = []
    cursors = []

    def respond(request):
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        paths.append(request.url.raw_path.split(b"?")[0])
        if request.url.path.endswith("/me"):
            assert request.url.params["fields"] == "id,username"
            return httpx.Response(200, json=PROFILE)
        assert request.url.params["limit"] == "50"
        cursor = request.url.params.get("after")
        cursors.append(cursor)
        if request.url.path.endswith("/next/conversation"):
            return httpx.Response(
                200, json={"data": [], "paging": {"cursors": {"after": "unused"}}}
            )
        rows = (
            [
                ROW,
                {**ROW, "is_reply_owned_by_me": True},
                {**ROW, "username": "owner", "is_reply_owned_by_me": False},
                {**ROW, "text": "  "},
            ]
            if cursor is None
            else [{**ROW, "id": "second"}]
        )
        return httpx.Response(
            200, json={"data": rows, "paging": {"cursors": {"after": "same"}}}
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = ThreadsEngagementClient(http, TOKEN)
        replies = client.conversation("post/escaped")
        assert client.conversation("next") == []

    assert replies == [
        Reply(
            reply_id,
            "Hello",
            "reader",
            datetime(2026, 11, 20, 12, tzinfo=UTC),
            ROW["permalink"],
        )
        for reply_id in ("reply", "second")
    ]
    assert paths == [
        b"/v1.0/me",
        b"/v1.0/post%2Fescaped/conversation",
        b"/v1.0/post%2Fescaped/conversation",
        b"/v1.0/next/conversation",
    ]
    assert cursors == [None, "same", None]


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(401, text=TOKEN),
        httpx.Response(429, text=TOKEN),
        httpx.Response(500, text=TOKEN),
        httpx.Response(200, text=TOKEN),
        httpx.Response(200, json={"data": {}}),
        httpx.Response(
            200, json={"data": [{**ROW, "timestamp": "2026-11-20T12:00:00"}]}
        ),
        httpx.Response(200, json={"data": [{**ROW, "timestamp": TOKEN}]}),
        httpx.Response(200, json={"data": [{**ROW, "id": 123}]}),
        httpx.Response(200, json={"data": [{**ROW, "id": ""}]}),
    ],
)
def test_request_and_shape_errors_hide_sensitive_data(failure):
    def respond(request):
        return (
            httpx.Response(200, json=PROFILE)
            if request.url.path.endswith("/me")
            else failure
        )

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
        pytest.raises(ThreadsEngagementError) as error,
    ):
        ThreadsEngagementClient(http, TOKEN).conversation("post")
    assert TOKEN not in str(error.value)
    assert error.value.__cause__ is None


def test_conversation_stops_at_page_cap():
    count = 0

    def respond(request):
        nonlocal count
        if request.url.path.endswith("/me"):
            return httpx.Response(200, json=PROFILE)
        count += 1
        return httpx.Response(
            200,
            json={"data": [ROW], "paging": {"cursors": {"after": str(count)}}},
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        assert (
            len(ThreadsEngagementClient(http, TOKEN).conversation("post")) == count == 5
        )
