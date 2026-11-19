import json
import logging
import traceback
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import create_autospec
from uuid import uuid4

import httpx
import pytest
from channel_contract import ChannelContract

from postpilot.channels.base import ChannelError, TransientChannelError, marker
from postpilot.channels.linkedin import LinkedInChannel
from postpilot.core.models import Item, Publication, PublicationStatus, Variant
from postpilot.core.publisher import Publisher
from postpilot.core.states import ItemState
from postpilot.store.repo import Claim, ItemRepo

TOKEN = "private-access-token"
TEXT = "private-approved-commentary"


class LinkedInServer:
    def __init__(self):
        self.posts = {}
        self.requests = []
        self.status = 201
        self.status_by_lang = {}
        self.hide_posts = False
        self.lose_response = False

    def __call__(self, request):
        self.requests.append(request)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if request.url.path == "/v2/userinfo":
            return httpx.Response(200, json={"sub": "member"})
        assert request.url.path == "/rest/posts"
        if request.method == "GET":
            assert request.url.params["q"] == "author"
            assert request.url.params["author"] == "urn:li:person:member"
            return httpx.Response(
                200,
                json={"elements": [] if self.hide_posts else list(self.posts.values())},
            )
        payload = json.loads(request.content)
        lang = payload["commentary"].rsplit("/", 1)[-1]
        status = self.status_by_lang.get(lang, self.status)
        if status == "transport":
            raise httpx.ReadError(f"{TOKEN} {TEXT}", request=request)
        if status != 201:
            return httpx.Response(status, text=f"{TOKEN} {TEXT}")
        remote_id = f"urn:li:share:{len(self.posts) + 1}"
        self.posts[remote_id] = {"id": remote_id, **payload}
        if self.lose_response:
            raise httpx.ReadError("Response lost", request=request)
        return httpx.Response(201, headers={"x-restli-id": remote_id})


@pytest.fixture
def server():
    return LinkedInServer()


@pytest.fixture
def client(server):
    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        yield client


class TestLinkedInChannel(ChannelContract):
    @pytest.fixture(params=["de", "ua"])
    def make_channel(self, request, client, server):
        channel = LinkedInChannel(request.param, request.param, lambda: TOKEN, client)
        self.server = server
        return channel

    def read_post(self, channel, remote_id):
        return self.server.posts[remote_id]["commentary"]

    def test_payload_headers_and_userinfo_cache(
        self, make_channel, item, monkeypatch, client, server
    ):
        monkeypatch.setenv("LINKEDIN_VERSION", "202608")
        lang = make_channel.name
        channel = LinkedInChannel(lang, lang, lambda: TOKEN, client)
        text = r"(Text) #tag @name @[Person](urn:li:person:123) *literal*"
        variants = [Variant(item.id, lang, text)]
        remote_id = channel.publish(item, variants)
        assert server.posts[remote_id] == {
            "id": remote_id,
            "author": "urn:li:person:member",
            "commentary": (
                r"\(Text\) #tag @name @[Person](urn:li:person:123) \*literal\*"
                + f"\n\n{marker(item.id)}/{lang}"
            ),
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        post = server.requests[-1]
        assert post.headers["linkedin-version"] == "202608"
        assert post.headers["x-restli-protocol-version"] == "2.0.0"
        assert channel.find_existing(item.id) == remote_id
        assert channel.find_existing(uuid4()) is None
        assert sum(r.url.path == "/v2/userinfo" for r in server.requests) == 1

    @pytest.mark.parametrize("text", ["", "   ", "a" * 2950])
    def test_invalid_text_is_rejected_without_http(
        self, make_channel, item, text, server
    ):
        with pytest.raises(ChannelError):
            make_channel.publish(item, [Variant(item.id, make_channel.name, text)])
        assert server.requests == []

    def test_limit_includes_footer(self, make_channel, item):
        make_channel.validate(item, [Variant(item.id, make_channel.name, "a" * 2949)])

    @pytest.mark.parametrize(
        ("status", "error"),
        [
            (400, ChannelError),
            (401, ChannelError),
            (403, ChannelError),
            (429, TransientChannelError),
            (500, TransientChannelError),
            ("transport", TransientChannelError),
        ],
    )
    def test_sanitized_failures(
        self, make_channel, item, server, status, error, caplog
    ):
        server.status = status
        with caplog.at_level(logging.DEBUG), pytest.raises(error) as exc:
            make_channel.publish(item, [Variant(item.id, make_channel.name, TEXT)])
        assert isinstance(exc.value, TransientChannelError) == (
            error is TransientChannelError
        )
        rendered = "".join(traceback.format_exception(exc.value))
        for private in (TOKEN, TEXT):
            assert private not in rendered
            assert private not in caplog.text

    def test_ambiguous_send_never_reposts_after_lookup_miss(
        self, make_channel, item, server
    ):
        repo = make_repo(item, [Variant(item.id, make_channel.name, TEXT)])
        row = Publication(
            item.id,
            make_channel.name,
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
        assert (
            publisher.publish_item(item.id).channels[make_channel.name].outcome
            == "pending"
        )
        server.hide_posts = True
        assert (
            publisher.publish_item(item.id).channels[make_channel.name].outcome
            == "pending"
        )
        server.hide_posts = False
        assert (
            publisher.publish_item(item.id).channels[make_channel.name].outcome
            == "published"
        )
        assert len(server.posts) == 1
        repo.mark_failed.assert_not_called()

    def test_lookup_distinguishes_both_languages(self, item, client):
        de = LinkedInChannel("linkedin", "de", lambda: TOKEN, client)
        ua = LinkedInChannel("linkedin-ua", "ua", lambda: TOKEN, client)
        variants = [Variant(item.id, language, TEXT) for language in ("de", "ua")]
        de_id = de.publish(item, variants)
        assert ua.find_existing(item.id) is None
        ua_id = ua.publish(item, variants)
        assert de.find_existing(item.id) == de_id
        assert ua.find_existing(item.id) == ua_id


def make_repo(item, variants):
    repo = create_autospec(ItemRepo, instance=True)
    repo.get_item.return_value = replace(item, state=ItemState.PUBLISHING)
    repo.get_variants.return_value = variants
    repo.list_publications.return_value = []
    return repo


def test_retries_only_the_rejected_variant(client, server):
    now = datetime.now(UTC)
    item = Item(uuid4(), "github", ItemState.PUBLISHING, now, now, 1, now)
    repo = make_repo(item, [Variant(item.id, lang, TEXT) for lang in ("de", "ua")])
    de = LinkedInChannel("linkedin", "de", lambda: TOKEN, client)
    ua = LinkedInChannel("linkedin-ua", "ua", lambda: TOKEN, client)
    de_row = Publication(
        item.id, de.name, PublicationStatus.PUBLISHING, None, None, now
    )
    ua_row = replace(de_row, channel=ua.name)
    repo.claim_publication.side_effect = [
        Claim(de_row, recovered=False),
        Claim(ua_row, recovered=False),
        Claim(de_row, recovered=True),
    ]
    server.status_by_lang["de"] = 403
    publisher = Publisher(repo, [de, ua])
    first = publisher.publish_item(item.id)
    assert first.channels[de.name].outcome == "failed"
    assert first.channels[ua.name].outcome == "published"

    ua_id = next(iter(server.posts))
    repo.list_publications.return_value = [
        replace(ua_row, status=PublicationStatus.PUBLISHED, remote_id=ua_id)
    ]
    server.status_by_lang["de"] = 201
    second = publisher.publish_item(item.id)
    assert second.channels[de.name].outcome == "published"
    assert second.channels[ua.name].outcome == "skipped"
    assert len(server.posts) == 2
