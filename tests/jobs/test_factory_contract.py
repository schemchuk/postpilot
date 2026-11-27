import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from unittest.mock import Mock
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest

from postpilot.channels.base import ChannelError, UncertainPublication
from postpilot.channels.facebook_reels import FacebookReelsChannel
from postpilot.channels.factory import (
    FactoryAssets,
    FactoryLinkedInChannel,
    FactoryReelsChannel,
    FactoryThreadsChannel,
)
from postpilot.channels.linkedin import LinkedInChannel
from postpilot.config import FacebookSettings
from postpilot.core.models import Item
from postpilot.core.publisher import Publisher
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.projects import FACTORY_PROJECT, Project, Projects
from postpilot.store.project_items import ProjectItem
from postpilot.video.label import AI_IMAGE_CAPTION_LABEL

NOW = datetime(2026, 12, 1, 7, tzinfo=UTC)
VIDEO = b"video"
COVER = b"\x89PNG\r\n\x1a\ncover"
REPO = "owner/repo"
PROJECTS = Projects((Project(FACTORY_PROJECT, REPO, ("reel",), ("threads",), "auto"),))
URL = f"https://github.com/{REPO}/releases/download/reel/002-test/reel_de.mp4"


COVER_URL = URL.replace("reel_de.mp4", "cover_de.png")


def item(state, source="project"):
    return Item(uuid4(), source, state, NOW, NOW, None, NOW)


@pytest.mark.parametrize("state", [ItemState.APPROVED, ItemState.PUBLISHING])
@pytest.mark.parametrize("release_at", [NOW, NOW + timedelta(seconds=1)])
def test_factory_publisher_leaves_unreleased_items_untouched(state, release_at):
    row = item(state)
    repo = Mock(**{"get_item.return_value": row})
    channel = Mock()
    report = Publisher(
        repo,
        [],
        project_channels=[channel],
        projects=PROJECTS,
        project_item=lambda _: ProjectItem(
            "clip", release_at, False, "text", None, {}, 30, project=FACTORY_PROJECT
        ),
        now=lambda: NOW - timedelta(seconds=1),
    ).publish_item(row.id)
    assert not report.channels
    repo.transition.assert_not_called()
    channel.validate.assert_not_called()


def test_factory_opt_in_does_not_bypass_approval_for_other_sources():
    factory = item(ItemState.APPROVED)
    telegram = item(ItemState.AWAITING_APPROVAL, "telegram")
    repo = Mock(**{"get_item.side_effect": [factory, telegram]})
    channel = Mock()
    with pytest.raises(IllegalTransition, match="project publisher"):
        Publisher(repo, [channel]).publish_item(factory.id)
    with pytest.raises(IllegalTransition, match="AWAITING_APPROVAL"):
        Publisher(
            repo,
            [channel],
            project_channels=[channel],
            now=lambda: NOW,
        ).publish_item(telegram.id)
    repo.transition.assert_not_called()
    channel.validate.assert_not_called()


def assets(
    client,
    *,
    text=' "Ein Post" [Test].\n ',
    thread=" Zweite Zeile.\n",
    size=5,
    cover=COVER,
):
    reel = ProjectItem(
        "002-test",
        NOW,
        False,
        text,
        thread,
        {
            "reel_de.mp4": {"sha256": sha256(VIDEO).hexdigest(), "size": size},
            "cover_de.png": {"sha256": sha256(cover).hexdigest(), "size": len(cover)},
        },
        30,
        project=FACTORY_PROJECT,
    )
    return FactoryAssets(Mock(**{"get_by_item.return_value": reel}), client, PROJECTS)


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
    calls = []

    def route(request):
        calls.append(request.url.host)
        if request.url.host == "api.github.com":
            return httpx.Response(404)
        assert request.method == "GET"
        assert request.url.path.endswith("/saved-video")
        return httpx.Response(
            200, json={"status": {"publishing_phase": {"status": "complete"}}}
        )

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        channel = FactoryReelsChannel(
            FacebookReelsChannel(FacebookSettings("page"), client),
            lambda _: "token",
            assets(client),
        )
        save_ref = Mock()
        assert (
            channel.publish_with_ref(
                item(ItemState.PUBLISHING),
                [],
                ref="saved-video",
                claimed_at=NOW,
                save_ref=save_ref,
            )
            == "saved-video"
        )
    assert calls == ["api.github.com", "graph.facebook.com"]
    save_ref.assert_not_called()


@pytest.mark.parametrize(
    ("mode", "content", "reason"),
    [
        ("ok", COVER, None),
        ("hash", COVER[:-1] + b"X", "changed since intake"),
        ("size", COVER + b"X", "changed since intake"),
        ("missing", COVER, "is missing cover_de.png"),
        ("signature", b"invalid", "PNG signature"),
    ],
)
def test_factory_cover_checks_and_caches_release_asset(mode, content, reason):
    requests = []

    def route(request):
        requests.append(str(request.url))
        if request.url.host == "api.github.com":
            files = [("reel_de.mp4", URL, VIDEO)]
            if mode != "missing":
                files.append(("cover_de.png", COVER_URL, content))
            return httpx.Response(
                200,
                json={
                    "assets": [
                        {"name": name, "browser_download_url": url, "size": len(data)}
                        for name, url, data in files
                    ]
                },
            )
        if str(request.url) == URL:
            return httpx.Response(200, content=VIDEO)
        assert str(request.url) == COVER_URL
        return httpx.Response(200, content=content)

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        fetcher = assets(client, cover=content if mode == "signature" else COVER)
        item_id = uuid4()
        assert fetcher.video(item_id) == VIDEO
        if mode != "missing":
            assert fetcher.cover_url(item_id) == COVER_URL
        for _ in range(2):
            if reason:
                with pytest.raises(ChannelError, match=reason):
                    fetcher.cover(item_id)
            else:
                assert fetcher.cover(item_id) == COVER
    assert len(requests) == (2 if mode == "missing" else 3)
    assert requests[1:] == ([URL] if mode == "missing" else [URL, COVER_URL])


POST_TEXT = "  (Klammern) [x] *y*\r\n"
LINKEDIN_COMMENTARY = r"  \(Klammern\) \[x\] \*y\*" + "\r\n"
THREAD_TEXT = "  Ein Thread.\n"
POST_FILES = {
    "post_de.txt": POST_TEXT.encode(),
    "post_threads.txt": THREAD_TEXT.encode(),
}
POST_PROJECTS = Projects(
    (
        Project(
            FACTORY_PROJECT,
            REPO,
            ("reel", "post"),
            ("linkedin-de", "threads", "instagram"),
            "auto",
        ),
    )
)


def scheduled_post(**changes):
    from dataclasses import replace

    return replace(
        ProjectItem(
            "test-post",
            NOW,
            True,
            POST_TEXT,
            THREAD_TEXT,
            {
                name: {"size": len(data), "sha256": sha256(data).hexdigest()}
                for name, data in POST_FILES.items()
            },
            None,
            FACTORY_PROJECT,
            kind="post",
        ),
        **changes,
    )


def post_asset_route(files, requests):
    urls = {
        f"https://github.com/{REPO}/releases/download/post/test-post/{name}": data
        for name, data in files.items()
    }

    def route(request):
        if request.url.host == "api.github.com":
            assert request.url.path.endswith("/tags/post/test-post")
            return httpx.Response(
                200,
                json={
                    "assets": [
                        {
                            "name": url.rsplit("/", 1)[1],
                            "size": len(data),
                            "browser_download_url": url,
                        }
                        for url, data in urls.items()
                    ]
                },
            )
        requests.append(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, content=urls[str(request.url)])

    return route


@pytest.mark.parametrize("name", ["linkedin-video", "threads"])
def test_publish_project_post_verbatim_after_checking_text_assets(name):
    downloads = []
    asset_route = post_asset_route(POST_FILES, downloads)
    store = Mock()
    store.get_by_item.return_value = scheduled_post()
    remote = []

    def route(request):
        if request.url.host in {"api.github.com", "github.com"}:
            return asset_route(request)
        assert downloads == ["post_de.txt", "post_threads.txt"]
        remote.append(request.url.path)
        if request.url.path == "/v2/userinfo":
            return httpx.Response(200, json={"sub": "person"})
        assert request.method == "POST"
        if name == "linkedin-video":
            assert request.url.path == "/rest/posts"
            payload = json.loads(request.content)
            assert payload["commentary"] == LINKEDIN_COMMENTARY
            assert "content" not in payload
            return httpx.Response(201, headers={"x-restli-id": "post"})
        if request.url.path.endswith("/me/threads"):
            assert parse_qs(request.content.decode()) == {
                "media_type": ["TEXT"],
                "text": [THREAD_TEXT],
            }
            return httpx.Response(200, json={"id": "container"})
        assert request.url.path.endswith("/me/threads_publish")
        assert parse_qs(request.content.decode()) == {"creation_id": ["container"]}
        return httpx.Response(200, json={"id": "post"})

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        reader = FactoryAssets(store, client, POST_PROJECTS)
        channel = (
            FactoryLinkedInChannel(
                LinkedInChannel(name, "de", lambda: "token", client), reader
            )
            if name == "linkedin-video"
            else FactoryThreadsChannel(lambda: "token", client, reader)
        )
        assert channel.name == name
        save_ref = Mock()
        assert (
            channel.publish_with_ref(
                item(ItemState.PUBLISHING),
                [],
                ref=None,
                claimed_at=NOW,
                save_ref=save_ref,
            )
            == "post"
        )
    assert len(remote) == 2
    save_ref.assert_called_once_with(
        "text-posting" if name == "linkedin-video" else "container"
    )


@pytest.mark.parametrize("missing", [False, True])
def test_project_post_release_check_rejects_changed_or_missing_text(missing):
    files = POST_FILES.copy()
    name = "post_threads.txt"
    if missing:
        del files[name]
    else:
        files[name] = b"x" * len(files[name])
    store = Mock()
    store.get_by_item.return_value = scheduled_post()
    with httpx.Client(
        transport=httpx.MockTransport(post_asset_route(files, []))
    ) as client:
        reader = FactoryAssets(store, client, POST_PROJECTS)
        error = f"missing {name}" if missing else f"{name} changed since intake"
        with pytest.raises(ChannelError, match=error):
            reader.check_release(uuid4())


@pytest.mark.parametrize("match_count", [0, 1, 2])
def test_uncertain_linkedin_text_post_only_looks_up_on_retry(match_count):
    store = Mock()
    store.get_by_item.return_value = scheduled_post()
    asset_route = post_asset_route(POST_FILES, [])
    posts_sent = []
    published_at = int(NOW.timestamp() * 1000)

    def route(request):
        if request.url.host in {"api.github.com", "github.com"}:
            return asset_route(request)
        if request.url.path == "/v2/userinfo":
            return httpx.Response(200, json={"sub": "person"})
        assert request.url.path == "/rest/posts"
        if request.method == "POST":
            posts_sent.append(request)
            return httpx.Response(503)
        assert request.url.params["q"] == "author"
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "id": "old",
                        "commentary": LINKEDIN_COMMENTARY,
                        "publishedAt": published_at - 1,
                    },
                    {
                        "id": "video",
                        "commentary": LINKEDIN_COMMENTARY,
                        "publishedAt": published_at,
                        "content": {"media": {"id": "video"}},
                    },
                    *[
                        {
                            "id": f"remote-{index}",
                            "commentary": LINKEDIN_COMMENTARY,
                            "publishedAt": published_at,
                        }
                        for index in range(match_count)
                    ],
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        channel = FactoryLinkedInChannel(
            LinkedInChannel("linkedin-video", "de", lambda: "token", client),
            FactoryAssets(store, client, POST_PROJECTS),
        )
        row = item(ItemState.PUBLISHING)
        save_ref = Mock()
        with pytest.raises(UncertainPublication):
            channel.publish_with_ref(
                row, [], ref=None, claimed_at=NOW, save_ref=save_ref
            )
        save_ref.assert_called_once_with("text-posting")
        assert channel.publish_with_ref(
            row, [], ref=save_ref.call_args.args[0], claimed_at=NOW, save_ref=save_ref
        ) == ("remote-0" if match_count == 1 else None)
    assert len(posts_sent) == 1


@pytest.mark.parametrize("threads_skipped", [False, True])
def test_publisher_limits_post_to_text_channels(monkeypatch, threads_skipped):
    from types import SimpleNamespace

    from postpilot.core.publisher import ChannelResult

    row = item(ItemState.PUBLISHING)
    repo = Mock()
    repo.get_item.return_value = row
    repo.list_publications.return_value = []
    channels = [
        SimpleNamespace(name=name)
        for name in ("linkedin-video", "threads", "instagram-reels")
    ]
    publisher = Publisher(
        repo,
        [],
        project_channels=channels,
        project_item=lambda _: scheduled_post(threads_skipped=threads_skipped),
        projects=POST_PROJECTS,
        now=lambda: NOW,
    )
    send = Mock(return_value=ChannelResult("published"))
    monkeypatch.setattr(publisher, "_publish_channel", send)
    expected = channels[:1] if threads_skipped else channels[:2]
    assert set(publisher.publish_item(row.id).channels) == {
        target.name for target in expected
    }
    assert [call.args[0] for call in send.call_args_list] == expected


def test_publisher_refuses_post_for_reel_only_project():
    row = item(ItemState.APPROVED)
    repo = Mock()
    repo.get_item.return_value = row
    publisher = Publisher(
        repo,
        [],
        project_channels=[Mock()],
        project_item=lambda _: scheduled_post(),
        projects=PROJECTS,
        now=lambda: NOW,
    )
    with pytest.raises(IllegalTransition, match="cannot publish post"):
        publisher.publish_item(row.id)
    repo.transition.assert_not_called()


IMAGE_FILES = POST_FILES | {"image.png": COVER}
IMAGE_URL = f"https://github.com/{REPO}/releases/download/post/test-post/image.png"
IMAGE_URN = "urn:li:image:new"
OLD_IMAGE_URN = "urn:li:image:old"


def image_post_store(ai=True):
    store = Mock()
    post = scheduled_post(ai=ai)
    store.get_by_item.return_value = scheduled_post(
        ai=ai,
        assets=post.assets
        | {"image.png": {"size": len(COVER), "sha256": sha256(COVER).hexdigest()}},
    )
    return store


@pytest.mark.parametrize(
    "resume", [None, "AVAILABLE", "WAITING_UPLOAD", "missing", "uncertain"]
)
@pytest.mark.parametrize("ai", [True, False])
def test_linkedin_image_publication_and_recovery(resume, ai, caplog):
    asset_route = post_asset_route(IMAGE_FILES, [])
    row = item(ItemState.PUBLISHING)
    save_ref = Mock()
    uploaded = []
    posts = []
    text = LINKEDIN_COMMENTARY + ("\n\n" + AI_IMAGE_CAPTION_LABEL if ai else "")
    expected_urn = OLD_IMAGE_URN if resume == "AVAILABLE" else IMAGE_URN

    def route(request):
        if request.url.host in {"api.github.com", "github.com"}:
            return asset_route(request)
        if request.url.path == "/v2/userinfo":
            return httpx.Response(200, json={"sub": "person"})
        if request.method == "PUT":
            assert save_ref.call_args.args == (f"image:{IMAGE_URN}",)
            assert request.headers["Authorization"] == "Bearer token"
            assert request.content == COVER
            uploaded.append(request)
            return httpx.Response(201)
        assert request.headers["LinkedIn-Version"] == "202607"
        assert request.headers["X-Restli-Protocol-Version"] == "2.0.0"
        if request.url.path == "/rest/images":
            assert request.method == "POST"
            assert dict(request.url.params) == {"action": "initializeUpload"}
            assert json.loads(request.content) == {
                "initializeUploadRequest": {"owner": "urn:li:person:person"}
            }
            return httpx.Response(
                200,
                json={
                    "value": {
                        "image": IMAGE_URN,
                        "uploadUrl": "https://www.linkedin.com/upload?secret-signature=123",
                    }
                },
            )
        if request.url.path.startswith("/rest/images/"):
            assert request.method == "GET"
            if request.url.path.endswith(OLD_IMAGE_URN):
                if resume == "missing":
                    return httpx.Response(404)
                return httpx.Response(200, json={"status": resume})
            assert request.url.path.endswith(IMAGE_URN)
            return httpx.Response(200, json={"status": "AVAILABLE"})
        assert request.url.path == "/rest/posts"
        if request.method == "POST":
            posts.append(request)
            content = json.loads(request.content)
            assert content["commentary"] == text
            assert content["visibility"] == "PUBLIC"
            assert content["distribution"]["feedDistribution"] == "MAIN_FEED"
            assert content["content"] == {"media": {"id": expected_urn}}
            if resume == "uncertain":
                return httpx.Response(503)
            return httpx.Response(201, headers={"x-restli-id": "post"})
        assert resume == "uncertain"
        assert request.url.params["q"] == "author"
        published_at = int(NOW.timestamp() * 1000)
        return httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "id": "old",
                        "commentary": text,
                        "publishedAt": published_at - 1,
                        "content": {"media": {"id": expected_urn}},
                    },
                    {
                        "id": "wrong-image",
                        "commentary": text,
                        "publishedAt": published_at,
                        "content": {"media": {"id": OLD_IMAGE_URN}},
                    },
                    {
                        "id": "post",
                        "commentary": text,
                        "publishedAt": published_at,
                        "content": {"media": {"id": expected_urn}},
                    },
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        channel = FactoryLinkedInChannel(
            LinkedInChannel(
                "linkedin-video", "de", lambda: "token", client, version="202607"
            ),
            FactoryAssets(image_post_store(ai), client, POST_PROJECTS),
        )
        ref = f"image:{OLD_IMAGE_URN}" if resume not in {None, "uncertain"} else None
        if resume == "uncertain":
            with pytest.raises(UncertainPublication):
                channel.publish_with_ref(
                    row, [], ref=ref, claimed_at=NOW, save_ref=save_ref
                )
            ref = save_ref.call_args.args[0]
            assert ref == f"image-posting:{IMAGE_URN}"
        with caplog.at_level("INFO", logger="httpx"):
            assert (
                channel.publish_with_ref(
                    row, [], ref=ref, claimed_at=NOW, save_ref=save_ref
                )
                == "post"
            )
    assert "secret-signature" not in caplog.text
    assert len(posts) == 1
    assert len(uploaded) == (0 if resume == "AVAILABLE" else 1)


@pytest.mark.parametrize("ai", [True, False])
@pytest.mark.parametrize("outcome", ["FINISHED", "ERROR", "PUBLISHED", "IN_PROGRESS"])
def test_threads_image_publication_and_recovery(ai, outcome):
    asset_route = post_asset_route(IMAGE_FILES, [])
    save_ref = Mock()
    sleep = Mock()
    lookup_count = 0
    text = THREAD_TEXT + ("\n\n" + AI_IMAGE_CAPTION_LABEL if ai else "")

    def route(request):
        nonlocal lookup_count
        if request.url.host in {"api.github.com", "github.com"}:
            return asset_route(request)
        path = request.url.path
        if path.endswith("/container"):
            assert outcome == "PUBLISHED" or save_ref.call_args.args == ("container",)
            assert request.url.params["fields"] == "status,error_message"
            lookup_count += 1
            return httpx.Response(
                200,
                json={
                    "status": "IN_PROGRESS"
                    if lookup_count == 1 and outcome != "PUBLISHED"
                    else outcome,
                    "error_message": "Failed downloading image",
                },
            )
        if path.endswith("/me/threads") and request.method == "GET":
            assert outcome == "PUBLISHED"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "post", "text": text, "timestamp": NOW.isoformat()},
                    ]
                },
            )
        assert request.method == "POST" and outcome != "PUBLISHED"
        if path.endswith("/me/threads"):
            assert parse_qs(request.content.decode()) == {
                "media_type": ["IMAGE"],
                "text": [text],
                "image_url": [IMAGE_URL],
            }
            return httpx.Response(200, json={"id": "container"})
        assert outcome == "FINISHED" and path.endswith("/me/threads_publish")
        assert parse_qs(request.content.decode()) == {"creation_id": ["container"]}
        return httpx.Response(200, json={"id": "post"})

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        channel = FactoryThreadsChannel(
            lambda: "token",
            client,
            FactoryAssets(image_post_store(ai), client, POST_PROJECTS),
            sleep=sleep,
        )
        kwargs = {
            "ref": "container" if outcome == "PUBLISHED" else None,
            "claimed_at": NOW,
            "save_ref": save_ref,
        }
        row = item(ItemState.PUBLISHING)
        if outcome == "ERROR":
            with pytest.raises(ChannelError, match="Failed downloading image"):
                channel.publish_with_ref(row, [], **kwargs)
        else:
            assert channel.publish_with_ref(row, [], **kwargs) == (
                None if outcome == "IN_PROGRESS" else "post"
            )
    assert lookup_count == {"PUBLISHED": 1, "IN_PROGRESS": 6}.get(outcome, 2)
    assert sleep.call_count == lookup_count - 1


@pytest.mark.parametrize("name", ["linkedin-video", "threads"])
@pytest.mark.parametrize("deleted", [False, True])
def test_image_release_fails_before_channel_calls(name, deleted):
    asset_route = post_asset_route(IMAGE_FILES | {"image.png": COVER[:-1] + b"!"}, [])

    def route(request):
        assert request.url.host in {"api.github.com", "github.com"}
        if deleted:
            return httpx.Response(404)
        return asset_route(request)

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        reader = FactoryAssets(image_post_store(), client, POST_PROJECTS)
        channel = (
            FactoryLinkedInChannel(
                LinkedInChannel(name, "de", lambda: "token", client), reader
            )
            if name == "linkedin-video"
            else FactoryThreadsChannel(lambda: "token", client, reader)
        )
        save_ref = Mock()
        reason = (
            "release post/test-post is unavailable"
            if deleted
            else "image.png changed since intake"
        )
        with pytest.raises(ChannelError, match=reason):
            channel.publish_with_ref(
                item(ItemState.PUBLISHING),
                [],
                ref=None,
                claimed_at=NOW,
                save_ref=save_ref,
            )
        save_ref.assert_not_called()


def test_threads_checks_image_again_after_linkedin_reads_it():
    row = item(ItemState.PUBLISHING)
    changed = False

    def route(request):
        assert request.url.host in {"api.github.com", "github.com"}
        if changed and str(request.url) == IMAGE_URL:
            return httpx.Response(200, content=COVER[:-1] + b"!")
        return post_asset_route(IMAGE_FILES, [])(request)

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        assets = FactoryAssets(image_post_store(), client, POST_PROJECTS)
        assets.check_release(row.id)
        changed = True
        channel = FactoryThreadsChannel(lambda: "token", client, assets)
        with pytest.raises(ChannelError, match="image.png changed since intake"):
            channel.publish_with_ref(row, [], ref=None, claimed_at=NOW, save_ref=Mock())
