from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
import test_store

from postpilot.channels.linkedin_video import LinkedInVideoChannel
from postpilot.config import (
    FacebookSettings,
    InstagramSettings,
    LinkedInSettings,
    Settings,
    ThreadsSettings,
)
from postpilot.core.models import PublicationStatus
from postpilot.core.states import ItemState
from postpilot.jobs import tasks
from postpilot.store.own_videos import OwnVideoStore

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn
repo = test_store.repo


@pytest.mark.parametrize("first", ["text", "reels"])
@pytest.mark.parametrize("duration", [30, 2, 91])
def test_own_video_jobs_share_targets_and_resume_saved_ref(
    repo, conn, monkeypatch, first, duration
):
    # The payload is not a real MP4; ffmpeg normalisation is covered elsewhere.
    monkeypatch.setattr(
        "postpilot.channels.own_video_reels._fit_video", lambda video, scale: video
    )
    item = repo.create_item("own_video")
    text = "Ein Satz. " + "Wort " * 550
    repo.add_variant(item.id, "ua", "Український текст.")
    repo.add_variant(item.id, "de", text)
    repo.transition(item.id, ItemState.AWAITING_APPROVAL)
    repo.transition(item.id, ItemState.APPROVED, approved_by=1)
    store = OwnVideoStore(conn)
    with conn.transaction():
        video_id = store.add(
            1,
            {
                "file_id": "file",
                "file_unique_id": str(uuid4()),
                "duration": duration,
                "width": 1080,
                "height": 1920,
            },
            False,
            "interview",
        )
        store.finish(video_id, item.id)
    assert (video := store.get_by_item(item.id)).width == 1080
    assert video.height == 1920

    settings = Settings(
        "db",
        "bot",
        1,
        "key",
        linkedin=LinkedInSettings("id", "secret", "uri", ()),
        threads=ThreadsSettings("id", "secret", "uri"),
        facebook=FacebookSettings("fb"),
        instagram=InstagramSettings("ig"),
    )
    monkeypatch.setattr(LinkedInVideoChannel, "validate", lambda *args: None)
    monkeypatch.setattr(
        LinkedInVideoChannel, "publish_with_ref", lambda *args, **kwargs: "linkedin"
    )
    token = SimpleNamespace(
        access_token="token", expires_at=datetime.now(UTC) + timedelta(days=1)
    )
    monkeypatch.setattr(
        tasks, "TokenStore", Mock(**{"return_value.get.return_value": token})
    )
    monkeypatch.setattr(tasks, "TokenCipher", Mock())
    alerts = Mock()
    monkeypatch.setattr(tasks, "Notifier", Mock(**{"return_value.send_alert": alerts}))
    reel_calls = []
    threads_calls = []

    def route(request):
        path = request.url.path
        form = parse_qs(request.content.decode())
        if request.url.host == "graph.threads.com":
            threads_calls.append(path)
            if path.endswith("/threads"):
                assert form == {"media_type": ["TEXT"], "text": ["Ein Satz.…"]}
                return httpx.Response(200, json={"id": "thread-container"})
            assert form == {"creation_id": ["thread-container"]}
            return httpx.Response(200, json={"id": "thread"})
        reel_calls.append(path)
        assert duration == 30
        if path.endswith("/getFile"):
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "clip.mp4"}}
            )
        if path.endswith("/clip.mp4"):
            return httpx.Response(200, content=b"test")
        if path.endswith("/saved-fb"):
            assert request.method == "GET"
            return httpx.Response(
                200, json={"status": {"publishing_phase": {"status": "complete"}}}
            )
        if path.endswith("/ig/media"):
            assert len(form["caption"][0]) <= 2200
            return httpx.Response(200, json={"id": "ig-container"})
        if request.url.host == "rupload.facebook.com":
            return httpx.Response(200, json={"success": True})
        if path.endswith("/ig-container"):
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "ig-post"})
        assert path.endswith("/ig-post")
        return httpx.Response(
            200, json={"permalink": "https://www.instagram.com/reel/test"}
        )

    client = httpx.Client
    transport = httpx.MockTransport(route)
    monkeypatch.setattr(tasks.httpx, "Client", lambda: client(transport=transport))

    def run_text_job():
        with client(transport=transport) as session:
            publisher = tasks._publisher(settings, repo, session)
            tasks._publish_items(publisher, [item.id])

    def run_reels_job():
        tasks.publish_reels(settings, conn)

    steps = (
        (run_text_job, run_reels_job)
        if first == "text"
        else (run_reels_job, run_text_job)
    )
    if duration == 30:
        repo.transition(item.id, ItemState.PUBLISHING)
        claim = repo.claim_publication(item.id, "facebook-reels")
        repo.save_publication_ref(claim, "saved-fb")
        with conn.transaction():
            conn.execute(
                "UPDATE publications SET updated_at = now() - interval '16 minutes' WHERE item_id = %s",
                (item.id,),
            )
    steps[0]()
    expected = (
        ItemState.PUBLISHED
        if first == "text" and duration != 30
        else ItemState.PUBLISHING
    )
    assert repo.get_item(item.id).state == expected

    steps[1]()
    assert repo.get_item(item.id).state == ItemState.PUBLISHED
    rows = repo.list_publications(item.id)
    targets = {"linkedin", "threads"}
    if duration == 30:
        targets.update(("instagram-reels", "facebook-reels"))
        alerts.assert_any_call(
            "instagram-reels: published https://www.instagram.com/reel/test"
        )
        alerts.assert_any_call(
            "facebook-reels: published https://www.facebook.com/reel/saved-fb"
        )
    else:
        assert not reel_calls
        alerts.assert_not_called()
    assert {row.channel for row in rows} == targets
    assert all(row.status == PublicationStatus.PUBLISHED for row in rows)
    assert threads_calls == ["/v1.0/me/threads", "/v1.0/me/threads_publish"]
