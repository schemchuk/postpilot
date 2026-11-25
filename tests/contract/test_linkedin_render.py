from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from test_linkedin_video import POST_ID, URN, VideoServer

from postpilot.channels.base import ChannelError, UncertainPublication
from postpilot.channels.linkedin import LinkedInChannel
from postpilot.channels.linkedin_video import LinkedInRenderChannel
from postpilot.store.repo import ItemRepo
from postpilot.video.label import AI_AVATAR_CAPTION_LABEL
from postpilot.video.publications import VideoPublicationStore
from postpilot.video.publisher import publish_one
from postpilot.video.store import VideoRender, VideoStatus, VideoStore


@pytest.mark.parametrize(
    "name,lang,text",
    [("linkedin-video", "de", "Deutsch"), ("linkedin-ua-video", "ua", "Українська")],
)
def test_render_upload_and_recovery_keep_video_and_text_markers_distinct(
    name, lang, text
):
    server = VideoServer()
    server.item = replace(server.item, source="github")
    repo = Mock(spec=ItemRepo)
    repo.get_item.return_value = server.item
    repo.get_variants.return_value = server.variants
    item_id = server.item.id
    now = server.item.updated_at
    render = VideoRender(
        uuid4(),
        item_id,
        "de",
        "heygen",
        VideoStatus.APPROVED,
        "Avatar script",
        server.data,
        20,
        now,
        123,
        now,
        True,
    )
    text_footer = f"postpilot:{item_id}/{lang}"
    server.posts.append({"id": "text-post", "commentary": f"{text}\n\n{text_footer}"})
    server.status = "PROCESSING"

    def token_source():
        return "token"

    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        linkedin = LinkedInChannel(
            name, lang, token_source, client, "202611", video_marker=True
        )
        text_channel = LinkedInChannel(
            name.removesuffix("-video"), lang, token_source, client, "202611"
        )
        channel = LinkedInRenderChannel(linkedin, repo, sleep=server.sleeps.append)
        publish = channel.publish_render
        save_ref = server.events.append
        assert publish(render, "token", None, save_ref) is None
        assert server.events[:6] == [
            "initializeUpload",
            f"video:{URN}",
            "part-0",
            "part-1",
            "part-2",
            "finalizeUpload",
        ]
        assert f"video-final:{URN}" in server.events
        assert len(server.posts) == 1

        server.status = "AVAILABLE"
        server.lose_id = True
        # A finalized URN can be resumed without reading the video bytes again.
        resumed = replace(render, video=None)
        server.events.clear()
        with pytest.raises(UncertainPublication):
            publish(resumed, "token", f"video-final:{URN}", save_ref)
        assert server.events == ["poll", "find", f"video-posting:{URN}", "post"]
        assert len(server.posts) == 2
        video = server.posts[-1]
        assert video["commentary"] == (
            f"{AI_AVATAR_CAPTION_LABEL}\n\n\\({text}\\)\n\n{text_footer}/video"
        )
        assert video["content"] == {"media": {"id": URN}}

        # Put the video first so lookup must skip it to find the text post.
        server.posts.reverse()
        assert text_channel.find_existing(item_id) == "text-post"
        assert publish(resumed, "token", f"video-posting:{URN}", save_ref) == POST_ID
        assert len(server.posts) == 2


@pytest.mark.parametrize("limit,length", [(3000, 2950), (100, 100)])
def test_long_render_commentary_fails_publication_and_alerts(limit, length):
    server = VideoServer()
    variant = replace(server.variants[0], text="a" * length)
    now = server.item.updated_at
    render = VideoRender(
        uuid4(),
        server.item.id,
        "de",
        "heygen",
        VideoStatus.APPROVED,
        "Avatar script",
        server.data,
        20,
        now,
        123,
        now,
        True,
    )
    repo = Mock(spec=ItemRepo)
    repo.get_item.return_value = server.item
    repo.get_variants.return_value = [variant]
    renders = Mock(spec=VideoStore)
    renders.get.return_value = render
    store = Mock(spec=VideoPublicationStore)
    store.oldest_eligible.return_value = render.id
    claim = store.claim.return_value
    claim.remote_ref = None
    notify = Mock()

    def token_source():
        return "token"

    with httpx.Client(transport=httpx.MockTransport(server)) as client:
        linkedin = LinkedInChannel(
            "linkedin-video",
            "de",
            token_source,
            client,
            "202611",
            limit=limit,
            video_marker=True,
        )
        with pytest.raises(
            ChannelError, match="^Text exceeds the LinkedIn length limit$"
        ):
            linkedin._commentary(
                server.item, [variant], leading_label=AI_AVATAR_CAPTION_LABEL
            )
        channel = LinkedInRenderChannel(linkedin, repo)
        publish_one(store, renders, [channel], lambda _: token_source(), notify)

    store.mark_failed.assert_called_once_with(
        claim, "ChannelError: Text exceeds the LinkedIn length limit"
    )
    notify.assert_called_once_with(
        "linkedin-video: ChannelError: Text exceeds the LinkedIn length limit"
    )
    store.save_ref.assert_not_called()
    store.mark_published.assert_not_called()
    assert not server.events
    assert not server.posts
