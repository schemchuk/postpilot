import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
import test_store

from postpilot.config import (
    FacebookSettings,
    InstagramSettings,
    LinkedInSettings,
    Settings,
)
from postpilot.core.models import PublicationStatus
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.jobs import tasks
from postpilot.store.factory_reels import FactoryReel, FactoryReelStore
from postpilot.video.label import AI_VIDEO_CAPTION_LABEL

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn
repo = test_store.repo

NOW = datetime(2026, 12, 1, 7, tzinfo=UTC)
VIDEO = b"factory mp4"
COVER = b"\x89PNG\r\n\x1a\ncover"
POST = "  Die Biene.\n\nZweite Zeile.  \n"
THREAD = "  Ein eigener Text.\n"


def add_reel(conn, *, release_at=NOW - timedelta(seconds=1), ai=True, threads=THREAD):
    reel = FactoryReel(
        f"002-{uuid4().hex}",
        release_at,
        ai,
        POST,
        threads,
        {
            "reel_de.mp4": {"sha256": sha256(VIDEO).hexdigest(), "size": len(VIDEO)},
            "cover_de.png": {"sha256": sha256(COVER).hexdigest(), "size": len(COVER)},
        },
        30,
    )
    assert FactoryReelStore(conn).create_factory_item(reel) == "new"
    with conn.transaction():
        return conn.execute(
            "SELECT item_id FROM factory_reels WHERE episode = %s", (reel.episode,)
        ).fetchone()[0]


@pytest.fixture
def job(monkeypatch, conn):
    state = SimpleNamespace(
        failure=None, downloads=0, posts={}, notices=[], retry=None, release_reads=0
    )
    state.settings = Settings(
        "db",
        "bot",
        1,
        "key",
        linkedin=LinkedInSettings("id", "secret", "uri", ()),
        facebook=FacebookSettings("fb"),
        instagram=InstagramSettings("ig"),
    )
    asset_url = f"https://github.com/{state.settings.factory_repo}/releases/download/clip/reel_de.mp4"

    cover_url = asset_url.replace("reel_de.mp4", "cover_de.png")

    def post(channel, text):
        state.posts.setdefault(channel, []).append(text)

    def route(request):
        path = request.url.path
        host = request.url.host
        if path.endswith("/fb-reel/thumbnails"):
            assert request.method == "POST"
            assert "facebook-reels" in state.posts
            assert b'name="source"; filename="cover_de.png"' in request.content
            assert COVER in request.content
            return httpx.Response(200, json={"success": True})
        form = parse_qs(request.content.decode()) if host.startswith("graph.") else {}
        if host == "api.github.com":
            assert "/releases/tags/reel%2F002-" in request.url.raw_path.decode()
            state.release_reads += 1
            if state.failure == "release":
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "assets": [
                        {
                            "name": name,
                            "size": len(content),
                            "browser_download_url": url,
                        }
                        for name, content, url, missing in [
                            ("reel_de.mp4", VIDEO, asset_url, "asset"),
                            ("cover_de.png", COVER, cover_url, "cover-asset"),
                        ]
                        if state.failure != missing
                    ]
                },
            )
        if str(request.url) == asset_url:
            state.downloads += 1
            return httpx.Response(
                200, content=b"X" + VIDEO[1:] if state.failure == "hash" else VIDEO
            )
        if str(request.url) == cover_url:
            return httpx.Response(
                200,
                content=COVER[:-1] + b"X" if state.failure == "cover-hash" else COVER,
            )
        if path.endswith("/userinfo"):
            return httpx.Response(200, json={"sub": "person"})
        if host == "api.linkedin.com" and path.endswith("/videos"):
            payload = json.loads(request.content)
            if request.url.params["action"] == "initializeUpload":
                assert payload["initializeUploadRequest"]["uploadThumbnail"] is True
                return httpx.Response(
                    200,
                    json={
                        "value": {
                            "video": "urn:li:video:1",
                            "uploadToken": "upload-token",
                            "thumbnailUploadUrl": "https://upload.example/thumbnail",
                            "uploadInstructions": [
                                {
                                    "uploadUrl": "https://upload.example/video",
                                    "firstByte": 0,
                                    "lastByte": len(VIDEO) - 1,
                                }
                            ],
                        }
                    },
                )
            assert payload["finalizeUploadRequest"]["uploadedPartIds"] == ["etag"]
            return httpx.Response(200, json={})
        if host == "upload.example":
            assert request.method == "PUT"
            if path == "/thumbnail":
                assert request.content == COVER
                return httpx.Response(201)
            assert request.content == VIDEO
            return httpx.Response(200, headers={"ETag": "etag"})
        if path.endswith("/videos/urn:li:video:1"):
            return httpx.Response(200, json={"status": "AVAILABLE"})
        if host == "api.linkedin.com" and path.endswith("/posts"):
            post("linkedin-video", json.loads(request.content)["commentary"])
            return httpx.Response(201, headers={"x-restli-id": "urn:li:share:1"})
        if host == "graph.threads.com":
            if path.endswith("/me/threads"):
                post("threads", form["text"][0])
                return httpx.Response(200, json={"id": "thread-container"})
            if path.endswith("/threads_publish"):
                return httpx.Response(200, json={"id": "thread"})
            assert path.endswith("/thread")
            return httpx.Response(
                200, json={"permalink": "https://www.threads.com/@user/post/test"}
            )
        if path.endswith("/ig/media"):
            if state.failure == "instagram":
                return httpx.Response(
                    400, json={"error": {"message": "test rejection"}}
                )
            assert form["cover_url"] == [cover_url]
            assert form["thumb_offset"] == ["0"]
            post("instagram-reels", form["caption"][0])
            return httpx.Response(200, json={"id": "ig-container"})
        if path.endswith("/fb/video_reels"):
            if form["upload_phase"] == ["start"]:
                return httpx.Response(200, json={"video_id": "fb-reel"})
            post("facebook-reels", form["description"][0])
            return httpx.Response(200, json={"success": True})
        if host == "rupload.facebook.com":
            assert request.content == b"fitted video"
            return httpx.Response(200, json={"success": True})
        if path.endswith("/ig-container"):
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "ig-post"})
        assert path.endswith("/ig-post"), str(request.url)
        return httpx.Response(
            200, json={"permalink": "https://www.instagram.com/reel/test"}
        )

    state.fit = Mock(return_value=b"fitted video")
    monkeypatch.setattr("postpilot.channels.factory._fit_video", state.fit)
    monkeypatch.setattr(tasks, "_now", lambda: NOW)
    monkeypatch.setattr(tasks, "TokenCipher", Mock())
    token = SimpleNamespace(access_token="token", expires_at=NOW + timedelta(days=1))
    monkeypatch.setattr(
        tasks, "TokenStore", Mock(**{"return_value.get.return_value": token})
    )
    api = Mock()
    api.send_message.side_effect = lambda *args, **kwargs: state.notices.append(args)
    monkeypatch.setattr(tasks, "TelegramApi", Mock(return_value=api))
    state.ai = Mock(
        side_effect=AssertionError("factory publishing must not call Claude")
    )
    monkeypatch.setattr(tasks, "ClaudeClient", state.ai)
    client = httpx.Client
    state.client = lambda: client(transport=httpx.MockTransport(route))
    monkeypatch.setattr(tasks.httpx, "Client", state.client)
    state.run = lambda: tasks.publish_reels(state.settings, conn)
    return state


@pytest.mark.parametrize("ai, threads", [(True, THREAD), (False, None)])
def test_due_factory_reels_preserve_text_and_skip_threads(repo, conn, job, ai, threads):
    due = add_reel(conn, ai=ai, threads=threads)
    future = add_reel(conn, release_at=NOW + timedelta(minutes=1))
    resumed_future = add_reel(conn, release_at=NOW + timedelta(minutes=1))
    repo.transition(resumed_future, ItemState.PUBLISHING)

    job.run()
    assert repo.get_item(due).state == ItemState.PUBLISHED
    assert repo.get_item(future).state == ItemState.APPROVED
    assert repo.get_item(resumed_future).state == ItemState.PUBLISHING
    assert (
        repo.list_publications(future) == repo.list_publications(resumed_future) == []
    )
    caption = POST + (f"\n\n{AI_VIDEO_CAPTION_LABEL}" if ai else "")
    expected = {
        "linkedin-video": [caption],
        "instagram-reels": [caption],
        "facebook-reels": [caption],
    }
    if threads:
        expected["threads"] = [threads]
    assert job.posts == expected
    assert all(
        row.status == PublicationStatus.PUBLISHED for row in repo.list_publications(due)
    )
    assert job.downloads == job.release_reads == 1
    job.fit.assert_called_once_with(VIDEO, scale=False)
    assert len(job.notices) == len(expected)
    assert all(": published https://" in args[1] for args in job.notices)
    job.ai.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ("hash", "Factory asset reel_de.mp4 changed since intake"),
        ("asset", "is missing reel_de.mp4"),
        ("cover-hash", "Factory asset cover_de.png changed since intake"),
        ("cover-asset", "is missing cover_de.png"),
        ("release", "is unavailable"),
    ],
)
def test_factory_asset_failure_sends_retry_skip(repo, conn, job, failure, reason):
    item_id = add_reel(conn, threads=None if failure.startswith("cover-") else THREAD)
    job.failure = failure
    with pytest.raises(tasks._ChannelPublishingFailed):
        job.run()
    assert repo.get_item(item_id).state == ItemState.FAILED
    assert not job.posts
    assert job.release_reads == 1
    [notice] = job.notices
    assert reason in notice[1]
    assert notice[2]["inline_keyboard"] == [
        [
            {"text": "Retry", "callback_data": f"retry:{item_id}"},
            {"text": "Skip", "callback_data": f"skipfail:{item_id}"},
        ]
    ]
    job.run()
    assert job.notices == [notice]


def test_partial_publish_can_retry_in_reels_job(repo, conn, job, monkeypatch):
    item_id = add_reel(conn)
    job.failure = "instagram"
    with pytest.raises(tasks._ChannelPublishingFailed):
        job.run()
    assert repo.get_item(item_id).state == ItemState.FAILED
    assert set(job.posts) == {"threads", "linkedin-video", "facebook-reels"}
    with job.client() as client:
        publisher = tasks._publisher(job.settings, repo, client)
        with pytest.raises(IllegalTransition, match="factory publisher"):
            publisher.publish_item(item_id)
    tasks.publish_due(job.settings, conn)
    assert repo.get_item(item_id).state == ItemState.FAILED

    def drain(inbox, handler, *, raise_errors):
        handler.handle_update(
            {
                "update_id": 1,
                "callback_query": {
                    "id": "callback",
                    "data": f"retry:{item_id}",
                    "from": {"id": 1},
                    "message": {"message_id": 42, "chat": {"id": 1}},
                },
            }
        )

    monkeypatch.setattr(tasks, "drain_inbox", drain)
    regular_publisher = Mock(
        side_effect=AssertionError("retry belongs to publish-reels")
    )
    monkeypatch.setattr(tasks, "_publisher", regular_publisher)
    tasks.poll_telegram(job.settings, conn)
    regular_publisher.assert_not_called()
    assert repo.get_item(item_id).state == ItemState.PUBLISHING
    assert set(job.posts) == {"threads", "linkedin-video", "facebook-reels"}

    job.failure = None
    job.run()
    assert repo.get_item(item_id).state == ItemState.PUBLISHED
    job.run()
    assert set(job.posts) == {
        "threads",
        "linkedin-video",
        "facebook-reels",
        "instagram-reels",
    }
    assert all(len(posts) == 1 for posts in job.posts.values())
    assert job.downloads == 2
    job.ai.assert_not_called()


def test_factory_text_and_linkedin_publish_without_meta(repo, conn, job):
    item_id = add_reel(conn)
    job.settings = replace(job.settings, instagram=None, facebook=None)
    job.run()
    assert repo.get_item(item_id).state == ItemState.PUBLISHED
    assert set(job.posts) == {"linkedin-video", "threads"}
    assert job.downloads == 1
    job.fit.assert_not_called()
