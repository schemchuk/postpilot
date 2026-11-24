from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest

from postpilot.channels.base import ChannelHeld
from postpilot.channels.instagram_reels import InstagramReelsChannel
from postpilot.channels.own_video_reels import OwnVideoReelsChannel
from postpilot.channels.registry import build_own_video_channels
from postpilot.channels.threads import ThreadsChannel
from postpilot.config import InstagramSettings
from postpilot.core.models import Item, Publication, PublicationStatus, Variant
from postpilot.core.publisher import Publisher
from postpilot.core.states import ItemState
from postpilot.notify.telegram import TelegramApi
from postpilot.store.own_videos import OwnVideo
from postpilot.store.repo import Claim
from postpilot.video.label import AI_VIDEO_CAPTION_LABEL


@pytest.fixture
def item():
    now = datetime.now(UTC)
    return Item(uuid4(), "own_video", ItemState.PUBLISHING, now, now, 1, now)


@pytest.mark.parametrize(
    "ai, size, long",
    [
        (False, (1080, 1920), False),
        (True, (1920, 1080), False),
        (True, (270, 480), True),
        (True, (None, None), True),
    ],
)
def test_own_video_instagram_caption_and_fit(item, monkeypatch, ai, size, long):
    video = OwnVideo(uuid4(), item.id, 1, "file", 30, ai, "", "generated", *size)
    text = "Ein vollständiger Satz. " + "Wort " * 550 if long else "Ein Beitrag."
    saved = []

    def ffmpeg(command, *, check, capture_output, timeout):
        assert command[0] == "ffmpeg"
        if size == (1080, 1920):
            assert "-vf" not in command
            assert command[command.index("-c:v") + 1] == "copy"
        else:
            assert "pad=1080:1920" in command[command.index("-vf") + 1]
        assert command[command.index("-ar") + 1] == "48000"
        assert "-t" not in command
        assert check and capture_output and timeout == 180
        assert Path(command[command.index("-i") + 1]).read_bytes() == b"original"
        Path(command[-1]).write_bytes(b"fitted")

    run = Mock(side_effect=ffmpeg)
    monkeypatch.setattr("postpilot.channels.own_video_reels.subprocess.run", run)

    def route(request):
        path = request.url.path
        if path.endswith("/getFile"):
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "clip.mp4"}}
            )
        if path.endswith("/clip.mp4"):
            return httpx.Response(200, content=b"original")
        if path.endswith("/ig/media"):
            form = parse_qs(request.content.decode())
            label = f"\n\n{AI_VIDEO_CAPTION_LABEL}" if ai else ""
            assert form["caption"] == [
                ("Ein vollständiger Satz.…" if long else text) + label
            ]
            assert len(form["caption"][0]) <= 2200
            assert form.get("is_ai_generated") == (["true"] if ai else None)
            return httpx.Response(200, json={"id": "container"})
        if request.url.host == "rupload.facebook.com":
            assert saved == ["container"]
            assert request.content == b"fitted"
            return httpx.Response(200, json={"success": True})
        if path.endswith("/container"):
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if path.endswith("/media_publish"):
            assert saved == ["container", "container"]
            return httpx.Response(200, json={"id": "post"})
        assert path.endswith("/post")
        return httpx.Response(
            200, json={"permalink": "https://www.instagram.com/reel/test"}
        )

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        channel = OwnVideoReelsChannel(
            InstagramReelsChannel(InstagramSettings("ig"), client),
            lambda _: video,
            TelegramApi("test", client=client),
            lambda _: "token",
            caption_limit=2200,
        )
        published = channel.publish_with_ref(
            item,
            [Variant(item.id, "de", text)],
            ref=None,
            claimed_at=item.updated_at,
            save_ref=saved.append,
        )
    assert published == "post"
    assert run.call_count == 1


@pytest.mark.parametrize("failure", ["getFile", "clip.mp4"])
def test_telegram_failure_marks_publication_failed_without_meta(item, failure):
    video = OwnVideo(uuid4(), item.id, 1, "file", 30, False, "", "generated")
    repo = Mock()
    repo.get_item.return_value = item
    repo.get_variants.return_value = [Variant(item.id, "de", "Ein Beitrag.")]
    repo.list_publications.return_value = []
    claim = Claim(
        Publication(
            item.id,
            "instagram-reels",
            PublicationStatus.PUBLISHING,
            None,
            None,
            item.updated_at,
        ),
        recovered=False,
    )
    repo.claim_publication.return_value = claim

    def route(request):
        assert request.url.host == "api.telegram.org"
        if request.url.path.endswith(failure):
            return httpx.Response(404)
        assert request.url.path.endswith("/getFile")
        return httpx.Response(
            200, json={"ok": True, "result": {"file_path": "clip.mp4"}}
        )

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        channel = OwnVideoReelsChannel(
            InstagramReelsChannel(InstagramSettings("ig"), client),
            lambda _: video,
            TelegramApi("test", client=client),
            lambda _: "token",
        )
        report = Publisher(repo, [], own_video_channels=[channel]).publish_item(item.id)
    assert report.channels[channel.name].outcome == "failed"
    repo.mark_failed.assert_called_once_with(
        item.id, channel.name, "Channel operation failed (ChannelError)", claim=claim
    )
    repo.save_publication_ref.assert_not_called()


def test_own_video_threads_uses_shortened_german_text_only(item):
    variants = [
        Variant(item.id, "ua", "Український текст."),
        Variant(item.id, "de", "Ein Satz. " + "Wort " * 110),
    ]
    saved = []

    def route(request):
        assert request.url.host == "graph.threads.com"
        form = parse_qs(request.content.decode())
        if request.url.path.endswith("/threads"):
            assert form == {"media_type": ["TEXT"], "text": ["Ein Satz.…"]}
            return httpx.Response(200, json={"id": "thread-container"})
        assert request.url.path.endswith("/threads_publish")
        assert form == {"creation_id": ["thread-container"]}
        assert saved == ["thread-container"]
        return httpx.Response(200, json={"id": "thread"})

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        threads = ThreadsChannel(lambda: "token", client)
        adapters = build_own_video_channels(
            [threads],
            fetch_video=Mock(side_effect=AssertionError("unexpected video retrieval")),
            ai_generated=Mock(
                side_effect=AssertionError("unexpected video inspection")
            ),
        )
        assert adapters == [threads]
        assert (
            threads.publish_with_ref(
                item,
                variants,
                ref=None,
                claimed_at=item.updated_at,
                save_ref=saved.append,
            )
            == "thread"
        )


def test_missing_reels_token_holds_before_telegram_or_meta(item):
    get_video, telegram, read_token = Mock(), Mock(), Mock(return_value=None)
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail())) as client:
        channel = OwnVideoReelsChannel(
            InstagramReelsChannel(InstagramSettings("ig"), client),
            get_video,
            telegram,
            read_token,
        )
        with pytest.raises(ChannelHeld):
            channel.publish_with_ref(
                item,
                [Variant(item.id, "de", "Ein Beitrag.")],
                ref=None,
                claimed_at=item.updated_at,
                save_ref=Mock(),
            )
    read_token.assert_called_once_with("instagram")
    get_video.assert_not_called()
    telegram.get_file.assert_not_called()
