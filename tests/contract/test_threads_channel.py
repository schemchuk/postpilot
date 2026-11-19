import logging
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

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
        self.statuses = {}
        self.before_publish = lambda ref: None
        self.lookup_posts = None

    def __call__(self, request):
        self.requests.append(request)
        assert request.url.host == "graph.threads.com"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url)
        path = request.url.path
        if request.method == "GET":
            if path != "/v1.0/me/threads":
                assert request.url.params["fields"] == "status"
                return httpx.Response(
                    200, json={"status": self.statuses[path.split("/")[-1]]}
                )
            assert request.url.params["fields"] == "id,text,timestamp"
            assert request.url.params["limit"] == "100"
            posts = [] if self.hide_posts else list(self.posts.values())
            if self.lookup_posts is not None:
                posts = self.lookup_posts
            if posts and "after" not in request.url.params:
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "other",
                                "text": "Different post",
                                "timestamp": datetime.now(UTC).isoformat(),
                            }
                        ],
                        "paging": {"cursors": {"after": "page-two"}},
                    },
                )
            if posts:
                assert request.url.params["after"] == "page-two"
            return httpx.Response(200, json={"data": posts})
        assert request.method == "POST"
        failure = self.failure if path.endswith("/threads_publish") else None
        if failure == "transport":
            raise httpx.ReadError(f"{TOKEN} {TEXT}", request=request)
        if failure:
            return httpx.Response(failure, text=f"{TOKEN} {TEXT}")
        form = parse_qs(request.content.decode())
        if path == "/v1.0/me/threads":
            assert form.keys() == {"media_type", "text"}
            assert form["media_type"] == ["TEXT"]
            creation_id = f"container-{len(self.containers) + 1}"
            self.containers[creation_id] = form["text"][0]
            self.statuses[creation_id] = "FINISHED"
            return httpx.Response(200, json={"id": creation_id})
        assert path == "/v1.0/me/threads_publish"
        assert form.keys() == {"creation_id"}
        remote_id = f"media-{len(self.posts) + 1}"
        ref = form["creation_id"][0]
        self.before_publish(ref)
        assert self.statuses[ref] == "FINISHED"
        self.statuses[ref] = "PUBLISHED"
        self.posts[remote_id] = {
            "id": remote_id,
            "text": self.containers[ref],
            "timestamp": datetime.now(UTC).isoformat(),
        }
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

    def test_publish_and_lookup(self, make_channel, item, server):
        variants = [Variant(item.id, "de", TEXT), Variant(item.id, "ua", "Допис")]
        refs = []

        def check_saved(ref):
            assert refs == [ref]

        server.before_publish = check_saved
        remote_id = make_channel.publish_with_ref(
            item, variants, ref=None, claimed_at=item.updated_at, save_ref=refs.append
        )
        assert self.read_post(make_channel, remote_id) == TEXT
        assert (
            make_channel.publish_with_ref(
                item,
                variants,
                ref=refs[0],
                claimed_at=item.updated_at,
                save_ref=refs.append,
            )
            == remote_id
        )
        assert refs == ["container-1"]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("a" * 500, "a" * 500),
            ("a" * 501, "a" * 499 + "…"),
            (
                "Erster Satz. Zweiter Satz! " + "Wort " * 100,
                "Erster Satz. Zweiter Satz!…",
            ),
            ("Wort " * 101, ("Wort " * 100).rstrip() + "…"),
            ("  Kurzer Text.  ", "  Kurzer Text.  "),
        ],
    )
    def test_text_and_publish_steps(self, make_channel, item, server, text, expected):
        variants = [Variant(item.id, "de", text)]
        make_channel.validate(item, variants)
        remote_id = make_channel.publish_with_ref(
            item,
            variants,
            ref=None,
            claimed_at=item.updated_at,
            save_ref=lambda _: None,
        )
        posted = self.read_post(make_channel, remote_id)
        assert posted == expected
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
                channel.publish_with_ref(
                    item,
                    [Variant(item.id, "de", TEXT)],
                    ref=None,
                    claimed_at=item.updated_at,
                    save_ref=lambda _: None,
                )
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
            make_channel.publish_with_ref(
                item,
                [Variant(item.id, "de", TEXT)],
                ref=None,
                claimed_at=item.updated_at,
                save_ref=lambda _: None,
            )
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
        first = Claim(row, recovered=False)
        stale = Claim(
            replace(
                row,
                remote_ref="container-1",
                remote_ref_claimed_at=row.updated_at,
                updated_at=row.updated_at + timedelta(minutes=15),
            ),
            recovered=True,
            stale=True,
        )
        repo.claim_publication.side_effect = [first, stale, stale]

        def check_saved(ref):
            repo.save_publication_ref.assert_called_once_with(first, ref)

        server.before_publish = check_saved
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

    @pytest.mark.parametrize(
        ("status", "outcome", "published_ref"),
        [
            (None, "published", "container-1"),
            ("FINISHED", "published", "container-1"),
            ("IN_PROGRESS", "pending", None),
            ("ERROR", "published", "container-2"),
            ("EXPIRED", "published", "container-2"),
            ("UNKNOWN", "pending", None),
        ],
    )
    def test_recovers_stale_container(
        self, make_channel, item, server, status, outcome, published_ref
    ):
        repo = make_repo(item, [Variant(item.id, "de", TEXT)])
        ref = "container-1" if status is not None else None
        if ref is not None:
            server.containers[ref] = TEXT
            server.statuses[ref] = status
        claim = Claim(
            Publication(
                item.id,
                "threads",
                PublicationStatus.PUBLISHING,
                None,
                None,
                item.updated_at,
                remote_ref=ref,
            ),
            recovered=True,
            stale=True,
        )
        repo.claim_publication.return_value = claim

        def check_saved(ref):
            repo.save_publication_ref.assert_called_once_with(claim, ref)
            assert ref == published_ref

        server.before_publish = check_saved
        report = Publisher(repo, [make_channel]).publish_item(item.id)
        assert report.channels["threads"].outcome == outcome
        assert len(server.posts) == int(published_ref is not None)
        assert len(server.containers) == (
            2
            if published_ref == "container-2"
            else int(ref is not None or published_ref is not None)
        )
        repo.mark_failed.assert_not_called()

    @pytest.mark.parametrize("matches", [0, 1, 2])
    def test_published_requires_a_single_exact_match_in_the_claim_window(
        self, make_channel, item, server, matches
    ):
        server.statuses["container-1"] = "PUBLISHED"
        timestamp = item.updated_at.isoformat()
        server.lookup_posts = [
            {"id": f"match-{i}", "text": TEXT, "timestamp": timestamp}
            for i in range(matches)
        ] + [
            {"id": "similar", "text": TEXT + " ", "timestamp": timestamp},
            {
                "id": "old",
                "text": TEXT,
                "timestamp": (item.updated_at - timedelta(minutes=6)).isoformat(),
            },
        ]
        found = make_channel.publish_with_ref(
            item,
            [Variant(item.id, "de", TEXT)],
            ref="container-1",
            claimed_at=item.updated_at,
            save_ref=lambda _: pytest.fail("Unexpected save"),
        )
        assert found == ("match-0" if matches == 1 else None)
        assert all(request.method == "GET" for request in server.requests)

    def test_published_matches_rounded_timestamp_and_stops_at_cutoff(
        self, make_channel, item, server
    ):
        claimed_at = item.updated_at.replace(microsecond=500_000)
        server.statuses["container-1"] = "PUBLISHED"
        server.lookup_posts = [
            {
                "id": "rounded",
                "text": TEXT,
                "timestamp": claimed_at.replace(microsecond=0).isoformat(),
            },
            {
                "id": "outside-margin",
                "text": TEXT,
                "timestamp": (claimed_at - timedelta(minutes=5, seconds=1))
                .replace(microsecond=0)
                .isoformat(),
            },
            {"id": "must-not-be-inspected", "text": TEXT},
        ]

        assert (
            make_channel.publish_with_ref(
                item,
                [Variant(item.id, "de", TEXT)],
                ref="container-1",
                claimed_at=claimed_at,
                save_ref=lambda _: pytest.fail("Unexpected save"),
            )
            == "rounded"
        )

    def test_token_change_after_save_prevents_publish(self, item, server):
        tokens = iter([TOKEN, TOKEN, "changed"])
        with httpx.Client(transport=httpx.MockTransport(server)) as client:
            channel = ThreadsChannel(lambda: next(tokens), client)
            refs = []
            with pytest.raises(ChannelHeld):
                channel.publish_with_ref(
                    item,
                    [Variant(item.id, "de", TEXT)],
                    ref=None,
                    claimed_at=item.updated_at,
                    save_ref=refs.append,
                )
        assert refs == ["container-1"]
        assert not server.posts

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
