from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from time import sleep
from urllib.parse import quote

import httpx

from postpilot.channels.base import (
    ChannelError,
    ChannelHeld,
    TransientChannelError,
    UncertainPublication,
)
from postpilot.channels.meta_reels import request
from postpilot.config import InstagramSettings
from postpilot.video.reel import ReelVideo

UPLOAD_ATTEMPTS = 5
UPLOAD_RETRY_SECONDS = 5


class InstagramReelsChannel:
    name = "instagram-reels"
    token_provider = "instagram"

    def __init__(
        self,
        settings: InstagramSettings,
        client: httpx.Client,
        *,
        sleep: Callable[[float], None] = sleep,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._request = partial(request, client, "Instagram Reels")
        self._account = self._graph(settings.account_id)
        self._sleep = sleep
        self._clock = clock or partial(datetime.now, UTC)
        self._permalinks: dict[str, str] = {}

    def _graph(self, node: str) -> str:
        return f"{self._settings.graph_url}/{quote(node, safe='')}"

    def permalink(self, remote_id: str) -> str:
        return self._permalinks.get(remote_id, f"instagram media {remote_id}")

    def validate(self, reel: ReelVideo) -> None:
        reel.validate("Instagram Reels", 900)

    def publish_with_ref(
        self,
        reel: ReelVideo,
        token: str,
        ref: str | None,
        save_ref: Callable[[str], None],
        *,
        claimed_at: datetime,
    ) -> str | None:
        self.validate(reel)
        caption = reel.caption
        deadline = self._clock() + timedelta(minutes=5)
        if ref is not None:
            status = self._poll(ref, token, deadline, claimed_at)
            if status == "IN_PROGRESS":
                return None
            if status not in ("ERROR", "EXPIRED"):
                if status != "PUBLISHED":
                    reel.video_bytes()
                return self._finish(status, ref, caption, token, claimed_at, save_ref)

        video = reel.video_bytes()
        # At most one new container per run, including when recovering an old one.
        container = self._request(
            "POST",
            f"{self._account}/media",
            token,
            data={
                "media_type": "REELS",
                "upload_type": "resumable",
                **({"caption": caption} if caption else {}),
                **({"is_ai_generated": "true"} if reel.ai_generated else {}),
            },
        ).get("id")
        if not isinstance(container, str) or not container:
            raise TransientChannelError("Instagram Reels did not return a container ID")
        save_ref(container)
        upload = (
            "https://rupload.facebook.com/ig-api-upload/"
            f"{self._settings.graph_version}/{quote(container, safe='')}"
        )
        self._upload(upload, token, video)
        status = self._poll(container, token, deadline)
        if status in ("FINISHED", "PUBLISHED"):
            return self._finish(status, container, caption, token, claimed_at, save_ref)
        return None

    def _upload(self, url: str, token: str, video: bytes) -> None:
        # The same container accepts a repeated upload from offset 0.
        for attempt in range(UPLOAD_ATTEMPTS):
            try:
                result = self._request("POST", url, token, video=video)
            except TransientChannelError:
                if attempt == UPLOAD_ATTEMPTS - 1:
                    raise
            else:
                if result.get("success") is True:
                    return
                if attempt == UPLOAD_ATTEMPTS - 1:
                    raise TransientChannelError(
                        "Instagram Reels upload was not confirmed"
                    )
            self._sleep(UPLOAD_RETRY_SECONDS)

    def _poll(
        self,
        ref: str,
        token: str,
        deadline: datetime,
        claimed_at: datetime | None = None,
    ) -> str:
        url = f"{self._graph(ref)}?fields=status_code,status"
        while True:
            status = self._request("GET", url, token).get("status_code")
            if status in ("FINISHED", "PUBLISHED", "ERROR", "EXPIRED"):
                return str(status)
            if status != "IN_PROGRESS":
                raise TransientChannelError(
                    "Instagram Reels container status unavailable"
                )
            now = self._clock()
            if claimed_at is not None and now - claimed_at > timedelta(hours=1):
                return "EXPIRED"
            remaining = (deadline - now).total_seconds()
            if remaining <= 0:
                return "IN_PROGRESS"
            self._sleep(min(10, remaining))

    def _finish(
        self,
        status: str,
        ref: str,
        caption: str,
        token: str,
        claimed_at: datetime,
        save_ref: Callable[[str], None],
    ) -> str:
        if status == "PUBLISHED":
            remote_id = self._find_published(caption, claimed_at, token)
        else:
            # Check claim ownership again before making the publishing call.
            save_ref(ref)
            result = self._request(
                "POST",
                f"{self._account}/media_publish",
                token,
                data={"creation_id": ref},
                finish=True,
            ).get("id")
            if not isinstance(result, str) or not result:
                raise UncertainPublication("Instagram Reels did not return a media ID")
            remote_id = result
        try:
            link = self._request(
                "GET", f"{self._graph(remote_id)}?fields=permalink", token
            )
        except (ChannelError, ChannelHeld):
            pass
        else:
            permalink = link.get("permalink")
            if isinstance(permalink, str) and permalink:
                self._permalinks[remote_id] = permalink
        return remote_id

    def _find_published(self, caption: str, claimed_at: datetime, token: str) -> str:
        cutoff = claimed_at - timedelta(minutes=5)
        endpoint = httpx.URL(f"{self._account}/media?fields=id,caption,timestamp")
        url = endpoint
        matches: set[str] = set()
        for _ in range(5):
            result = self._request("GET", str(url), token)
            posts = result.get("data")
            if not isinstance(posts, list):
                break
            for post in posts:
                if not isinstance(post, dict) or (post.get("caption") or "") != caption:
                    continue
                try:
                    posted_at = datetime.fromisoformat(post["timestamp"])
                    remote_id = post["id"]
                    if not isinstance(remote_id, str) or not remote_id:
                        raise ValueError
                    if posted_at >= cutoff:
                        matches.add(remote_id)
                except (KeyError, TypeError, ValueError):
                    raise UncertainPublication(
                        "Invalid Instagram media match"
                    ) from None
            paging = result.get("paging")
            next_page = paging.get("next") if isinstance(paging, dict) else None
            if not next_page:
                if len(matches) == 1:
                    return matches.pop()
                break
            # Rebuild the URL on the configured endpoint, dropping any access token.
            try:
                cursor = httpx.URL(next_page).params.get("after")
            except (TypeError, httpx.InvalidURL):
                break
            if not cursor or cursor == url.params.get("after"):
                break
            url = endpoint.copy_merge_params({"after": cursor})
        raise UncertainPublication("Instagram media could not be uniquely reconciled")
