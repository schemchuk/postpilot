from collections.abc import Callable
from datetime import datetime
from functools import partial
from urllib.parse import quote

import httpx

from postpilot.channels.base import (
    ChannelError,
    TransientChannelError,
    UncertainPublication,
)
from postpilot.channels.meta_reels import request
from postpilot.config import FacebookSettings
from postpilot.video.label import AI_AVATAR_CAPTION_LABEL
from postpilot.video.store import VideoRender, VideoStatus


class FacebookReelsChannel:
    name = "facebook-reels"
    token_provider = "facebook"

    def __init__(self, settings: FacebookSettings, client: httpx.Client) -> None:
        self._settings = settings
        self._request = partial(request, client, "Facebook Reels")
        self._endpoint = (
            f"{settings.graph_url}/{quote(settings.page_id, safe='')}/video_reels"
        )

    def permalink(self, remote_id: str) -> str:
        return f"https://www.facebook.com/reel/{quote(remote_id, safe='')}"

    def validate(self, render: VideoRender) -> None:
        if render.status != VideoStatus.APPROVED or not render.video:
            raise ChannelError("An approved video is required")
        if not render.script or not render.script.strip():
            raise ChannelError("A video script is required")
        if render.duration is not None and not 3 <= render.duration <= 90:
            raise ChannelError("Facebook Reels must be 3 to 90 seconds long")

    def publish_with_ref(
        self,
        render: VideoRender,
        token: str,
        ref: str | None,
        save_ref: Callable[[str], None],
        *,
        claimed_at: datetime,
    ) -> str | None:
        self.validate(render)
        assert render.video is not None
        offset = 0
        if ref is not None:
            result = self._request(
                "GET",
                f"{self._settings.graph_url}/{quote(ref, safe='')}?fields=status",
                token,
            )
            status = result.get("status")
            if not isinstance(status, dict):
                if result.get("error"):
                    status = {"error": result["error"]}
                else:
                    raise TransientChannelError("Facebook Reels status unavailable")
            upload, processing, publishing = (
                status.get(f"{phase}_phase") or {}
                for phase in ("uploading", "processing", "publishing")
            )
            if (
                not isinstance(upload, dict)
                or not isinstance(processing, dict)
                or not isinstance(publishing, dict)
            ):
                raise TransientChannelError("Invalid Facebook Reels phase")
            if publishing.get("status") == "complete":
                return ref
            if result.get("error") or any(
                phase.get("status") == "error"
                or phase.get("error")
                or phase.get("errors")
                for phase in (status, upload, processing, publishing)
            ):
                ref = None
            elif any(
                phase.get("status") == "in_progress"
                for phase in (processing, publishing)
            ):
                return None
            elif upload.get("status") != "complete":
                uploaded = upload.get(
                    "bytes_transferred", upload.get("bytes_transfered", 0)
                )
                if type(uploaded) is not int or not 0 <= uploaded <= len(render.video):
                    raise TransientChannelError("Invalid Facebook Reels upload offset")
                offset = uploaded
            elif publishing.get("status") == "not_started":
                offset = len(render.video)
            else:
                return None
        if ref is None:
            new_ref = self._request(
                "POST",
                self._endpoint,
                token,
                data={"upload_phase": "start"},
            ).get("video_id")
            if not isinstance(new_ref, str) or not new_ref:
                raise TransientChannelError("Facebook Reels did not return a video ID")
            ref = new_ref
            save_ref(ref)
        if offset < len(render.video):
            url = f"https://rupload.facebook.com/video-upload/{self._settings.graph_version}/{quote(ref, safe='')}"
            self._request("POST", url, token, video=render.video, offset=offset)
        result = self._request(
            "POST",
            self._endpoint,
            token,
            finish=True,
            data={
                "upload_phase": "finish",
                "video_id": ref,
                "video_state": "PUBLISHED",
                "description": f"{render.script}\n\n{AI_AVATAR_CAPTION_LABEL}",
            },
        )
        if result.get("success") is not True:
            raise UncertainPublication("Facebook Reels did not confirm publishing")
        return ref
