from datetime import UTC, datetime, timedelta
from hashlib import sha256
from unittest.mock import Mock
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest

from postpilot.channels.base import ChannelError
from postpilot.channels.facebook_reels import FacebookReelsChannel
from postpilot.channels.factory import (
    FactoryAssets,
    FactoryLinkedInChannel,
    FactoryThreadsChannel,
)
from postpilot.channels.linkedin import LinkedInChannel
from postpilot.config import FacebookSettings
from postpilot.core.models import Item
from postpilot.core.publisher import Publisher
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.store.factory_reels import FactoryReel
from postpilot.video.reel import ReelVideo

NOW = datetime(2026, 12, 1, 7, tzinfo=UTC)
VIDEO = b"video"
REPO = "owner/repo"
URL = f"https://github.com/{REPO}/releases/download/reel/002-test/reel_de.mp4"


def item(state, source="factory"):
    return Item(uuid4(), source, state, NOW, NOW, None, NOW)


@pytest.mark.parametrize("state", [ItemState.APPROVED, ItemState.PUBLISHING])
@pytest.mark.parametrize("release_at", [None, NOW + timedelta(seconds=1)])
def test_factory_publisher_leaves_unreleased_items_untouched(state, release_at):
    row = item(state)
    repo = Mock(**{"get_item.return_value": row})
    channel = Mock()
    report = Publisher(
        repo,
        [],
        factory_channels=[channel],
        factory_release_at=lambda _: release_at,
        now=lambda: NOW,
    ).publish_item(row.id)
    assert not report.channels
    repo.transition.assert_not_called()
    channel.validate.assert_not_called()


def test_factory_opt_in_does_not_bypass_approval_for_other_sources():
    factory = item(ItemState.APPROVED)
    telegram = item(ItemState.AWAITING_APPROVAL, "telegram")
    repo = Mock(**{"get_item.side_effect": [factory, telegram]})
    channel = Mock()
    with pytest.raises(IllegalTransition, match="factory publisher"):
        Publisher(repo, [channel]).publish_item(factory.id)
    with pytest.raises(IllegalTransition, match="AWAITING_APPROVAL"):
        Publisher(
            repo,
            [channel],
            factory_channels=[channel],
            factory_release_at=lambda _: NOW,
            now=lambda: NOW,
        ).publish_item(telegram.id)
    repo.transition.assert_not_called()
    channel.validate.assert_not_called()


def assets(client, *, text=' "Ein Post" [Test].\n ', thread=" Zweite Zeile.\n", size=5):
    reel = FactoryReel(
        "002-test",
        NOW,
        False,
        text,
        thread,
        {"reel_de.mp4": {"sha256": sha256(VIDEO).hexdigest(), "size": size}},
        30,
    )
    return FactoryAssets(Mock(**{"get_by_item.return_value": reel}), client, REPO)


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("ok", None),
        ("size", "changed since intake"),
        ("missing-release", "Factory release reel/002-test is unavailable"),
        ("missing-asset", "is missing reel_de.mp4"),
        ("disallowed", "disallowed download URL"),
        ("redirect", "disallowed download redirect"),
    ],
)
def test_factory_video_checks_and_caches_release_asset(mode, reason):
    requested = []

    def route(request):
        requested.append(str(request.url))
        if request.url.host == "api.github.com":
            assert request.url.raw_path.endswith(b"/releases/tags/reel%2F002-test")
            if mode == "missing-release":
                return httpx.Response(404)
            url = "https://evil.invalid/file" if mode == "disallowed" else URL
            return httpx.Response(
                200,
                json={
                    "assets": []
                    if mode == "missing-asset"
                    else [
                        {
                            "name": "reel_de.mp4",
                            "size": len(VIDEO),
                            "browser_download_url": url,
                        },
                        {
                            "name": "cover_de.png",
                            "size": 5,
                            "browser_download_url": "https://evil.invalid",
                        },
                    ]
                },
            )
        assert str(request.url) == URL
        if mode == "redirect":
            return httpx.Response(
                302, headers={"location": "https://evil.invalid/file"}
            )
        return httpx.Response(200, content=VIDEO)

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        fetcher = assets(client, size=len(VIDEO) + (mode == "size"))
        reel = item(ItemState.PUBLISHING)
        for _ in range(2):
            if reason:
                with pytest.raises(ChannelError, match=reason):
                    fetcher.video(reel.id)
            else:
                assert fetcher.video(reel.id) == VIDEO
    assert len(requested) == (2 if mode in {"ok", "size", "redirect"} else 1)


def test_linkedin_verbatim_and_uncertain_recovery_by_video_id():
    now = NOW
    row = item(ItemState.PUBLISHING)

    def route(request):
        if request.url.path.endswith("/userinfo"):
            return httpx.Response(200, json={"sub": "person"})
        assert request.method == "GET" and request.url.path.endswith("/posts")
        return httpx.Response(
            200,
            json={
                "elements": [
                    {"id": "unrelated", "content": {"media": {"id": "urn:li:video:9"}}},
                    {"id": "recovered", "content": {"media": {"id": "urn:li:video:1"}}},
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        channel = FactoryLinkedInChannel(
            LinkedInChannel("linkedin-video", "de", lambda: "token", client),
            assets(client),
        )
        assert channel._commentary(row, []) == ' "Ein Post" \\[Test\\].\n '
        save_ref = Mock()
        assert (
            channel.publish_with_ref(
                row,
                [],
                ref="video-posting:urn:li:video:1",
                claimed_at=now,
                save_ref=save_ref,
            )
            == "recovered"
        )
        save_ref.assert_not_called()


@pytest.mark.parametrize("thread", ["", " " * 8, "b" * 501])
def test_factory_threads_rejects_invalid_text_without_shortening(thread):
    with httpx.Client(
        transport=httpx.MockTransport(Mock(side_effect=AssertionError))
    ) as client:
        channel = FactoryThreadsChannel(
            lambda: "token", client, assets(client, thread=thread)
        )
        with pytest.raises(ChannelError, match="Factory Threads text"):
            channel.validate(item(ItemState.PUBLISHING), [])


def test_threads_only_checks_release_metadata_and_sends_stored_text():
    thread = "  Zeile eins.\nZeile zwei.  "
    callbacks = Mock()
    calls = []

    def route(request):
        calls.append(request.url.host)
        if request.url.host == "api.github.com":
            return httpx.Response(
                200,
                json={
                    "assets": [
                        {
                            "name": "reel_de.mp4",
                            "size": len(VIDEO),
                            "browser_download_url": URL,
                        }
                    ]
                },
            )
        assert request.url.host == "graph.threads.com"
        if request.url.path.endswith("/me/threads"):
            assert parse_qs(request.content.decode()) == {
                "media_type": ["TEXT"],
                "text": [thread],
            }
            return httpx.Response(200, json={"id": "container"})
        assert request.url.path.endswith("/me/threads_publish")
        return httpx.Response(200, json={"id": "thread"})

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        channel = FactoryThreadsChannel(
            lambda: "token", client, assets(client, thread=thread)
        )
        assert (
            channel.publish_with_ref(
                item(ItemState.PUBLISHING),
                [],
                ref=None,
                claimed_at=NOW,
                save_ref=callbacks,
            )
            == "thread"
        )
    callbacks.assert_called_once_with("container")
    assert calls == ["api.github.com", "graph.threads.com", "graph.threads.com"]


def test_facebook_recovers_a_published_reference_without_loading_release_bytes():
    load = Mock(side_effect=ChannelError("Factory release is unavailable"))
    reel = ReelVideo(load, "Caption", 30, False)

    def route(request):
        assert request.method == "GET"
        assert request.url.path.endswith("/saved-video")
        return httpx.Response(
            200, json={"status": {"publishing_phase": {"status": "complete"}}}
        )

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        channel = FacebookReelsChannel(FacebookSettings("page"), client)
        save_ref = Mock()
        assert (
            channel.publish_with_ref(
                reel, "token", "saved-video", save_ref, claimed_at=NOW
            )
            == "saved-video"
        )
    load.assert_not_called()
    save_ref.assert_not_called()
