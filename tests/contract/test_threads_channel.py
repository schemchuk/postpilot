import logging
import traceback
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from channel_contract import ChannelContract
from test_linkedin_channel import TOKEN as LINKEDIN_TOKEN
from test_linkedin_channel import LinkedInServer, make_repo

from postpilot.channels.base import (
    ChannelError,
    ChannelHeld,
    TransientChannelError,
    UncertainPublication,
    marker,
)
from postpilot.channels.linkedin import LinkedInChannel
from postpilot.channels.threads import ThreadsChannel
from postpilot.core.models import Publication, PublicationStatus, Variant
from postpilot.core.publisher import Publisher
from postpilot.store.repo import Claim

TOKEN = "mock-threads-token"
TEXT = "Mock approved text"


class ThreadsServer:
    def __init__(self):
        self.containers = {}
        self.posts = {}
        self.requests = []
        self.lose_response = False
        self.hide_posts = False
        self.failure = None

    def __call__(self, request):
        self.requests.append(request)
        assert request.url.host == "graph.threads.com"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url)
        path = request.url.path
        if request.method == "GET":
            assert path == "/v1.0/me/threads"
            assert request.url.params["fields"] == "id,text"
            assert request.url.params["limit"] == "100"
            posts = [] if self.hide_posts else list(self.posts.values())
            if posts and "after" not in request.url.params:
                return httpx.Response(
                    200,
                    json={
                        "data": [{"id": "other", "text": "Different post"}],
                        "paging": {"cursors": {"after": "page-two"}},
                    },
                )
            if posts:
                assert request.url.params["after"] == "page-two"
            return httpx.Response(200, json={"data": posts})
        assert request.method == "POST"
        if self.failure == "transport":
            raise httpx.ReadError(f"{TOKEN} {TEXT}", request=request)
        if self.failure:
            return httpx.Response(self.failure, text=f"{TOKEN} {TEXT}")
        form = parse_qs(request.content.decode())
        if path == "/v1.0/me/threads":
            assert form.keys() == {"media_type", "text"}
            assert form["media_type"] == ["TEXT"]
            creation_id = f"container-{len(self.containers) + 1}"
            self.containers[creation_id] = form["text"][0]
            return httpx.Response(200, json={"id": creation_id})
        assert path == "/v1.0/me/threads_publish"
        assert form.keys() == {"creation_id"}
        remote_id = f"media-{len(self.posts) + 1}"
        text = self.containers[form["creation_id"][0]]
        self.posts[remote_id] = {"id": remote_id, "text": text}
        if self.lose_response:
            raise httpx.ReadError("Response lost", request=request)
        return httpx.Response(200, json={"id": remote_id})


@pytest.fixture
def server():
    return ThreadsServer()


class TestThreadsChannel(ChannelContract):
    @pytest.fixture
    def make_channel(self, server):
        self.server = server
        with httpx.Client(transport=httpx.MockTransport(server)) as client:
            yield ThreadsChannel(lambda: TOKEN, client)

    def read_post(self, channel, remote_id):
        return self.server.posts[remote_id]["text"]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("a" * 449, "a" * 449),
            ("a" * 450, "a" * 448 + "…"),
            (
                "Erster Satz. Zweiter Satz! " + "Wort " * 100,
                "Erster Satz. Zweiter Satz!…",
            ),
            ("Wort " * 100, ("Wort " * 89).rstrip() + "…"),
            ("  Kurzer Text.  ", "  Kurzer Text.  "),
        ],
    )
    def test_text_and_publish_steps(self, make_channel, item, server, text, expected):
        variants = [Variant(item.id, "de", text)]
        make_channel.validate(item, variants)
        remote_id = make_channel.publish(item, variants)
        posted = self.read_post(make_channel, remote_id)
        assert posted == f"{expected}\n\n{marker(item.id)}/de"
        assert len(posted) <= 500
        assert [r.url.path for r in server.requests] == [
            "/v1.0/me/threads",
            "/v1.0/me/threads_publish",
        ]

    def test_invalid_text_and_missing_token(self, make_channel, item, server):
        with pytest.raises(ChannelError, match="empty"):
            make_channel.validate(item, [Variant(item.id, "de", "  ")])
        with pytest.raises(ChannelError, match="language"):
            make_channel.validate(item, [Variant(item.id, "ua", "Допис")])
        with httpx.Client(transport=httpx.MockTransport(server)) as client:
            channel = ThreadsChannel(lambda: None, client)
            with pytest.raises(ChannelHeld):
                channel.publish(item, [Variant(item.id, "de", TEXT)])
            with pytest.raises(ChannelHeld):
                channel.find_existing(uuid4())
        assert server.requests == []

    @pytest.mark.parametrize(
        ("failure", "error"),
        [
            (401, ChannelError),
            (403, ChannelError),
            (429, TransientChannelError),
            (500, UncertainPublication),
            ("transport", UncertainPublication),
        ],
    )
    def test_sanitized_failures(
        self, make_channel, item, server, failure, error, caplog
    ):
        server.failure = failure
        with caplog.at_level(logging.DEBUG), pytest.raises(error) as exc:
            make_channel.publish(item, [Variant(item.id, "de", TEXT)])
        assert type(exc.value) is error
        rendered = "".join(traceback.format_exception(exc.value))
        for private in (TOKEN, TEXT):
            assert private not in rendered
            assert private not in caplog.text

    def test_recovers_lost_publish_response_without_reposting(
        self, make_channel, item, server
    ):
        repo = make_repo(item, [Variant(item.id, "de", TEXT)])
        row = Publication(
            item.id,
            "threads",
            PublicationStatus.PUBLISHING,
            None,
            None,
            item.updated_at,
        )
        repo.claim_publication.side_effect = [
            Claim(row, recovered=False),
            Claim(row, recovered=True, stale=True),
            Claim(row, recovered=True, stale=True),
        ]
        server.lose_response = True
        publisher = Publisher(repo, [make_channel])
        assert publisher.publish_item(item.id).channels["threads"].outcome == "pending"
        server.hide_posts = True
        assert publisher.publish_item(item.id).channels["threads"].outcome == "pending"
        server.hide_posts = False
        assert (
            publisher.publish_item(item.id).channels["threads"].outcome == "published"
        )
        assert len(server.containers) == len(server.posts) == 1
        repo.mark_failed.assert_not_called()

    def test_threads_rejection_does_not_block_linkedin(
        self, make_channel, item, server
    ):
        repo = make_repo(item, [Variant(item.id, "de", TEXT)])
        repo.claim_publication.side_effect = [
            Claim(
                Publication(
                    item.id,
                    channel,
                    PublicationStatus.PUBLISHING,
                    None,
                    None,
                    item.updated_at,
                ),
                recovered=False,
            )
            for channel in ("threads", "linkedin")
        ]
        server.failure = 403
        linkedin_server = LinkedInServer()
        with httpx.Client(transport=httpx.MockTransport(linkedin_server)) as client:
            linkedin = LinkedInChannel("linkedin", "de", lambda: LINKEDIN_TOKEN, client)
            report = Publisher(repo, [make_channel, linkedin]).publish_item(item.id)
        assert report.channels["threads"].outcome == "failed"
        assert report.channels["linkedin"].outcome == "published"
        assert len(linkedin_server.posts) == 1
