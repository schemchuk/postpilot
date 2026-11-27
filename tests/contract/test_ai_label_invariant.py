import inspect
import json
import pkgutil
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from unittest.mock import Mock
from urllib.parse import parse_qsl
from uuid import uuid4

import httpx
import pytest
from test_linkedin_video import PNG, VideoServer
from test_tiktok_direct import CREATOR
from test_tiktok_upload import COMPLETE, INBOX, UPLOAD_URL, response
from test_youtube_upload import SESSION

from postpilot import channels
from postpilot.channels.base import ChannelError
from postpilot.channels.facebook_reels import FacebookReelsChannel
from postpilot.channels.factory import (
    FactoryAssets,
    FactoryLinkedInChannel,
    FactoryReelsChannel,
    FactoryThreadsChannel,
)
from postpilot.channels.instagram_reels import InstagramReelsChannel
from postpilot.channels.linkedin import LinkedInChannel
from postpilot.channels.linkedin_video import (
    LinkedInRenderChannel,
    LinkedInVideoChannel,
)
from postpilot.channels.own_video_reels import OwnVideoReelsChannel
from postpilot.channels.threads import ThreadsChannel
from postpilot.channels.tiktok import TikTokChannel
from postpilot.channels.youtube import YouTubeChannel
from postpilot.config import FacebookSettings, InstagramSettings
from postpilot.core.models import Item, Variant
from postpilot.core.states import ItemState
from postpilot.core.tiktok import TikTokChoice
from postpilot.notify.telegram import Notifier, TelegramApi
from postpilot.store.own_videos import OwnVideo
from postpilot.store.project_items import ProjectItem
from postpilot.store.repo import ItemRepo
from postpilot.video import pipeline
from postpilot.video.heygen import VideoResult
from postpilot.video.label import (
    AI_AVATAR_CAPTION_LABEL,
    AI_IMAGE_CAPTION_LABEL,
    AI_VIDEO_CAPTION_LABEL,
)
from postpilot.video.publisher import publish_one
from postpilot.video.reel import ReelVideo
from postpilot.video.store import VideoRender, VideoStatus

NOW = datetime(2026, 11, 27, tzinfo=UTC)
TEXT = "Ein freigegebener Beitrag."
VIDEO = b"video"
TOKEN = "token"


@dataclass(frozen=True)
class Payload:
    path: str
    caption: str
    flag: str | None = None
    target: str = ""


LINKEDIN = Payload("/rest/posts", "commentary")
THREADS = Payload("/me/threads", "text")
INSTAGRAM = Payload("/123/media", "caption", "is_ai_generated", "instagram")
FACEBOOK = Payload("/123/video_reels", "description", target="facebook")
YOUTUBE = Payload(
    "/upload/youtube/v3/videos",
    "snippet.description",
    "status.containsSyntheticMedia",
    "youtube",
)
DIRECT = Payload(
    "/publish/video/init/", "post_info.title", "post_info.is_aigc", "direct"
)
# ADR 026 delivers the inbox caption through Telegram. Inbox init only accepts upload data.
DRAFT = Payload("/sendMessage", "text", target="draft")
FALLBACK = Payload("/sendMessage", "text", target="fallback")


@dataclass(frozen=True)
class Case:
    adapter: type
    kind: str
    payload: Payload
    label: str | None = None
    position: str = "suffix"


# Each row names the entry point, accepted source, and expected wire disclosure.
# Threads receives only the S3 text variant, with no video attached.
CASES = (
    Case(LinkedInChannel, "text", LINKEDIN),
    Case(
        LinkedInVideoChannel, "own", LINKEDIN, AI_VIDEO_CAPTION_LABEL, "before marker"
    ),
    Case(LinkedInRenderChannel, "avatar", LINKEDIN, AI_AVATAR_CAPTION_LABEL, "leading"),
    Case(ThreadsChannel, "text", THREADS),
    Case(ThreadsChannel, "own", THREADS),
    Case(InstagramReelsChannel, "avatar", INSTAGRAM, AI_AVATAR_CAPTION_LABEL),
    Case(InstagramReelsChannel, "own", INSTAGRAM, AI_VIDEO_CAPTION_LABEL),
    Case(InstagramReelsChannel, "reel", INSTAGRAM, AI_VIDEO_CAPTION_LABEL),
    Case(FacebookReelsChannel, "avatar", FACEBOOK, AI_AVATAR_CAPTION_LABEL),
    Case(FacebookReelsChannel, "own", FACEBOOK, AI_VIDEO_CAPTION_LABEL),
    Case(FacebookReelsChannel, "reel", FACEBOOK, AI_VIDEO_CAPTION_LABEL),
    Case(YouTubeChannel, "own", YOUTUBE, AI_VIDEO_CAPTION_LABEL),
    Case(YouTubeChannel, "reel", YOUTUBE, AI_VIDEO_CAPTION_LABEL),
    Case(TikTokChannel, "own", DRAFT, AI_VIDEO_CAPTION_LABEL),
    Case(TikTokChannel, "own", DIRECT, AI_VIDEO_CAPTION_LABEL),
    Case(TikTokChannel, "reel", DRAFT, AI_VIDEO_CAPTION_LABEL),
    Case(TikTokChannel, "reel", DIRECT, AI_VIDEO_CAPTION_LABEL),
    Case(OwnVideoReelsChannel, "own", INSTAGRAM, AI_VIDEO_CAPTION_LABEL),
    Case(OwnVideoReelsChannel, "own", FACEBOOK, AI_VIDEO_CAPTION_LABEL),
    Case(OwnVideoReelsChannel, "own", YOUTUBE, AI_VIDEO_CAPTION_LABEL),
    Case(OwnVideoReelsChannel, "own", DRAFT, AI_VIDEO_CAPTION_LABEL),
    Case(OwnVideoReelsChannel, "own", DIRECT, AI_VIDEO_CAPTION_LABEL),
    Case(FactoryLinkedInChannel, "reel", LINKEDIN, AI_VIDEO_CAPTION_LABEL),
    Case(FactoryLinkedInChannel, "image", LINKEDIN, AI_IMAGE_CAPTION_LABEL),
    Case(FactoryLinkedInChannel, "project text", LINKEDIN),
    Case(FactoryThreadsChannel, "reel", THREADS, AI_VIDEO_CAPTION_LABEL),
    Case(FactoryThreadsChannel, "image", THREADS, AI_IMAGE_CAPTION_LABEL),
    Case(FactoryThreadsChannel, "project text", THREADS),
    Case(FactoryReelsChannel, "reel", INSTAGRAM, AI_VIDEO_CAPTION_LABEL),
    Case(FactoryReelsChannel, "reel", FACEBOOK, AI_VIDEO_CAPTION_LABEL),
    Case(FactoryReelsChannel, "reel", YOUTUBE, AI_VIDEO_CAPTION_LABEL),
    Case(FactoryReelsChannel, "reel", DRAFT, AI_VIDEO_CAPTION_LABEL),
    Case(FactoryReelsChannel, "reel", DIRECT, AI_VIDEO_CAPTION_LABEL),
    Case(FactoryReelsChannel, "reel", FALLBACK, AI_VIDEO_CAPTION_LABEL),
)


def test_every_channel_adapter_has_an_ai_label_case():
    excluded = {
        "base._Channel",  # Shared protocol.
        "base.MarkerChannel",  # Marker-based publication protocol.
        "base.RefChannel",  # Reference-based publication protocol.
        "fake.FakeChannel",  # In-memory test double with no HTTP payload.
        "linkedin_video._LinkedInVideoPublisher",  # Shared upload implementation.
    }
    discovered = set()
    for module_info in pkgutil.walk_packages(
        channels.__path__, f"{channels.__name__}."
    ):
        module = import_module(module_info.name)
        relative_name = module_info.name.removeprefix(f"{channels.__name__}.")
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__:
                continue
            if f"{relative_name}.{name}" in excluded:
                continue
            if any(
                callable(getattr(cls, method, None))
                for method in ("publish", "publish_with_ref", "publish_render")
            ):
                discovered.add(cls)

    missing = discovered - {case.adapter for case in CASES}
    assert not missing, "Add AI-label cases for: " + ", ".join(
        sorted(f"{cls.__module__}.{cls.__name__}" for cls in missing)
    )


def make_item(kind):
    source = {
        "avatar": "github",
        "own": "own_video",
        "reel": "project",
        "image": "project",
        "project text": "project",
        "text": "github",
    }[kind]
    item = Item(uuid4(), source, ItemState.APPROVED, NOW, NOW, 1, NOW)
    return item, [Variant(item.id, "de", TEXT)]


def project_assets(item, kind, ai, video):
    assets = (
        {"image.png": {"size": len(PNG), "sha256": "unused"}} if kind == "image" else {}
    )
    project = ProjectItem(
        "release",
        NOW,
        ai,
        TEXT,
        TEXT,
        assets,
        20 if kind == "reel" else None,
        "factory",
        item_id=item.id,
        kind="reel" if kind == "reel" else "post",
    )
    source = Mock(spec=FactoryAssets)
    source.item.return_value = source.reel.return_value = project
    source.video.return_value = source.fitted_video.return_value = video
    source.cover.return_value = PNG
    source.cover_url.return_value = source.image_url.return_value = (
        "https://assets.test/image.png"
    )
    source._download.return_value = PNG
    return source


class PublishingServer:
    def __init__(self, *, factory_video=False):
        self.requests = []
        self.linkedin = VideoServer()
        self.linkedin.data = VIDEO
        self.linkedin.bounds = [(0, 0), (1, 2), (3, 4)]
        self.linkedin.factory = factory_video

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        host = request.url.host
        if host == "api.linkedin.com":
            if path == "/rest/images":
                return httpx.Response(
                    200,
                    json={
                        "value": {
                            "image": "urn:li:image:test",
                            "uploadUrl": "https://www.linkedin.com/image",
                        }
                    },
                )
            if path.startswith("/rest/images/"):
                return httpx.Response(200, json={"status": "AVAILABLE"})
            return self.linkedin(request)
        if host == "www.linkedin.com" and path == "/image":
            assert request.content == PNG
            return httpx.Response(201)
        if host == "upload.test":
            return self.linkedin(request)
        if host == "graph.threads.com":
            if request.method == "GET":
                return httpx.Response(200, json={"status": "FINISHED"})
            return httpx.Response(200, json={"id": "thread"})
        if host == "rupload.facebook.com":
            assert request.content == VIDEO
            return httpx.Response(200, json={"success": True})
        if path.endswith(("/123/media", "/media_publish")):
            return httpx.Response(200, json={"id": "ig-video"})
        if path.endswith("/ig-video"):
            return httpx.Response(
                200,
                json={
                    "status_code": "FINISHED",
                    "permalink": "https://instagram.test/reel",
                },
            )
        if path.endswith("/video_reels"):
            form = dict(parse_qsl(request.content.decode()))
            if form["upload_phase"] == "start":
                return httpx.Response(200, json={"video_id": "fb-video"})
            return httpx.Response(200, json={"success": True})
        if path.endswith("/thumbnails"):
            return httpx.Response(200, json={"success": True})
        if host == "www.googleapis.com":
            if path.endswith("/upload/youtube/v3/videos"):
                if request.method == "POST":
                    return httpx.Response(200, headers={"Location": SESSION})
                return httpx.Response(201, json={"id": "video-id"})
            if path == "/youtube/v3/videos":
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {"processingDetails": {"processingStatus": "succeeded"}},
                        ]
                    },
                )
            if path.endswith("/thumbnails/set"):
                return httpx.Response(200, json={})
        if host == "open.tiktokapis.com":
            if path.endswith("/query/"):
                return response(**CREATOR)
            if path.endswith("/init/"):
                self.inbox = "/inbox/" in path
                return response(publish_id="tiktok-video", upload_url=UPLOAD_URL)
            if path.endswith("/fetch/"):
                return response(status=INBOX if self.inbox else COMPLETE)
        if str(request.url) == UPLOAD_URL:
            return httpx.Response(201)
        if host == "api.telegram.org" and path.endswith("/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        raise AssertionError(f"Unexpected request: {request.method} {host}")

    def payload(self, path, *, method="POST", phase=None):
        matches = [
            req
            for req in self.requests
            if req.method == method and req.url.path.endswith(path)
        ]
        bodies = [
            json.loads(req.content)
            if req.headers.get("content-type") == "application/json"
            else dict(parse_qsl(req.content.decode()))
            for req in matches
        ]
        if phase is not None:
            bodies = [body for body in bodies if body.get("upload_phase") == phase]
        assert len(bodies) == 1, f"Expected one payload for {path}, got {len(bodies)}"
        return bodies[0]


def reel_destination(target, client):
    if target == "instagram":
        return InstagramReelsChannel(InstagramSettings("123"), client)
    if target == "facebook":
        return FacebookReelsChannel(FacebookSettings("123"), client)
    if target == "youtube":
        return YouTubeChannel(client, clock=lambda: NOW)
    assert target in {"draft", "direct", "fallback"}
    return TikTokChannel(client, mode="draft" if target == "draft" else "direct")


def publish_avatar(destination, item, monkeypatch):
    renders = Mock()
    renders.render_lock.return_value = nullcontext(True)
    renders.rendering_ids.return_value = []
    renders.count_this_month.return_value = 0
    render_id = uuid4()
    renders.claim.return_value = render_id
    repo = Mock(spec=ItemRepo)
    repo.next_video_variant.return_value = Variant(item.id, "de", TEXT)
    heygen = Mock()
    heygen.wallet_balance.return_value = 5
    heygen.create_video.return_value = "heygen"
    heygen.wait_for_video.return_value = VideoResult(
        "captioned-url", None, None, None, 20
    )
    heygen.download.return_value = b"unlabelled render"
    burn = Mock(return_value=VIDEO)
    monkeypatch.setattr(pipeline, "burn_ai_label", burn)
    monkeypatch.setattr(pipeline, "generate_script", lambda *_: TEXT)
    pipeline.render_one(
        renders, repo, heygen, Mock(), Mock(), lambda: NOW, cap=4, min_balance=3
    )
    burn.assert_called_once_with(b"unlabelled render")

    # Carry the bytes saved by the render pipeline into the approved publication.
    renders.attach.assert_called_once()
    _, remote_id, script, video, duration = renders.attach.call_args.args
    renders.get.return_value = VideoRender(
        render_id,
        item.id,
        "de",
        remote_id,
        VideoStatus.APPROVED,
        script,
        video,
        duration,
        NOW,
        1,
        NOW,
    )
    publications = Mock()
    publications.claim.return_value.remote_ref = None
    publications.claim.return_value.updated_at = NOW
    publish_one(publications, renders, [destination], lambda _: TOKEN, Mock())
    publications.mark_published.assert_called_once()


def publish_case(case, ai, client, monkeypatch):
    item, variants = make_item(case.kind)

    def read_token(*_):
        return TOKEN

    ref_args = {"ref": None, "claimed_at": NOW, "save_ref": Mock()}
    adapter = case.adapter
    linkedin = LinkedInChannel(
        "linkedin",
        "de",
        read_token,
        client,
        "202611",
        video_marker=adapter is LinkedInRenderChannel,
    )
    if adapter is LinkedInChannel:
        assert linkedin.publish(item, variants)
        return
    if adapter is LinkedInVideoChannel:
        channel = adapter(
            linkedin, fetch_video=lambda _: VIDEO, ai_generated=lambda _: ai
        )
        assert channel.publish_with_ref(item, variants, **ref_args)
        return
    if adapter is LinkedInRenderChannel:
        repo = Mock(spec=ItemRepo)
        repo.get_item.return_value = item
        repo.get_variants.return_value = variants
        publish_avatar(adapter(linkedin, repo), item, monkeypatch)
        return
    if adapter is ThreadsChannel:
        assert adapter(read_token, client).publish_with_ref(item, variants, **ref_args)
        return
    if adapter in {FactoryLinkedInChannel, FactoryThreadsChannel}:
        assets = project_assets(item, case.kind, ai, VIDEO)
        if adapter is FactoryLinkedInChannel:
            channel = adapter(linkedin, assets)
        else:
            channel = adapter(read_token, client, assets)
        assert channel.publish_with_ref(item, variants, **ref_args)
        return

    target = case.payload.target
    destination = reel_destination(target, client)
    if adapter not in {OwnVideoReelsChannel, FactoryReelsChannel}:
        assert type(destination) is adapter
    if case.kind == "avatar":
        publish_avatar(destination, item, monkeypatch)
        return

    # The reel consumers receive a ReelVideo from these production entry points.
    # Use the same wrappers for consumer rows and wrapper rows so removing a label
    # upstream is observable in the captured downstream request.
    choice = TikTokChoice("SELF_ONLY") if target == "direct" else None
    telegram = TelegramApi(TOKEN, client=client)
    if case.kind == "own":
        video = OwnVideo(uuid4(), item.id, 1, "file", 20, ai, "description", "ready")
        monkeypatch.setattr(TelegramApi, "get_file", lambda *_: "file")
        monkeypatch.setattr(TelegramApi, "download_file", lambda *_: VIDEO)
        monkeypatch.setattr(
            "postpilot.channels.own_video_reels._fit_video", lambda data, **_: data
        )
        channel = OwnVideoReelsChannel(
            destination,
            lambda _: video,
            telegram,
            read_token,
            caption_limit=2200 if target == "instagram" else None,
            tiktok_choice=(lambda _: choice) if choice else None,
        )
    else:
        channel = FactoryReelsChannel(
            destination,
            read_token,
            project_assets(item, case.kind, ai, VIDEO),
            caption_limit=2200 if target == "instagram" else None,
            use_release_video=target in {"youtube", "direct", "draft", "fallback"},
            tiktok_choice=(lambda *_: choice)
            if target in {"direct", "fallback"}
            else None,
        )
    remote_id = channel.publish_with_ref(item, variants, **ref_args)
    assert remote_id
    if target in {"draft", "fallback"}:
        Notifier(telegram, 1).send_tiktok_draft(
            destination.caption(remote_id),
            fallback=target == "fallback",
        )


def field(body, path):
    for key in path.split("."):
        body = body.get(key)
        if body is None:
            break
    return body


@pytest.mark.parametrize(
    ("case", "ai"),
    [
        pytest.param(
            case,
            ai,
            id=f"{case.adapter.__name__}-{case.kind}-{case.payload.target or 'post'}-{'ai' if ai else 'plain'}",
        )
        for case in CASES
        for ai in (
            [True]
            if case.kind == "avatar"
            else [False]
            if case.kind == "text"
            else [False, True]
        )
    ],
)
def test_ai_label_in_outgoing_payload(case, ai, monkeypatch):
    server = PublishingServer(
        factory_video=case.adapter is FactoryLinkedInChannel and case.kind == "reel",
    )
    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        publish_case(case, ai, client, monkeypatch)

    wire = case.payload
    payload = server.payload(wire.path, phase="finish" if wire is FACEBOOK else None)
    caption = field(payload, wire.caption)
    assert TEXT in caption
    if wire is LINKEDIN:
        if case.kind in {"avatar", "own", "reel", "image"}:
            media = "image" if case.kind == "image" else "video"
            assert payload["content"]["media"]["id"].startswith(f"urn:li:{media}:")
        else:
            assert "content" not in payload
    if wire is THREADS:
        assert payload["media_type"] == ("IMAGE" if case.kind == "image" else "TEXT")
    if wire in (DRAFT, FALLBACK):
        inbox = server.payload("/publish/inbox/video/init/")
        assert inbox.keys() == {"source_info"}
    label = case.label if ai else None
    for candidate in (
        AI_AVATAR_CAPTION_LABEL,
        AI_VIDEO_CAPTION_LABEL,
        AI_IMAGE_CAPTION_LABEL,
    ):
        if candidate == label:
            assert caption.splitlines().count(candidate) == 1
        else:
            assert candidate not in caption
    if label is not None:
        if case.position == "leading":
            assert caption.startswith(f"{label}\n\n")
        elif case.position == "before marker":
            footer = caption.splitlines()[-1]
            assert footer.startswith("postpilot:")
            assert caption.endswith(f"\n\n{label}\n\n{footer}")
        else:
            assert caption.endswith(f"\n\n{label}")
    for flag_path in (INSTAGRAM.flag, YOUTUBE.flag, DIRECT.flag):
        flag = field(payload, flag_path)
        if ai and flag_path == wire.flag:
            if wire is INSTAGRAM:
                assert flag == "true"
            else:
                assert flag is True
        else:
            assert (
                flag is None
                or flag is False
                or (flag_path == INSTAGRAM.flag and flag == "false")
            )


@pytest.mark.parametrize(
    "target", ["instagram", "facebook", "youtube", "draft", "direct"]
)
def test_reel_adapters_reject_unlabelled_ai_video(target):
    def route(request):
        # Direct post checks creator capabilities before validating duration and caption.
        if target == "direct" and request.url.path.endswith("/creator_info/query/"):
            return response(**CREATOR)
        raise AssertionError(f"Unexpected request to {request.url.host}")

    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        channel = reel_destination(target, client)
        reel = ReelVideo(VIDEO, TEXT, 20, True, tiktok=TikTokChoice("SELF_ONLY"))
        with pytest.raises(ChannelError, match="AI-generated video label is required"):
            channel.publish_with_ref(reel, TOKEN, None, Mock(), claimed_at=NOW)
