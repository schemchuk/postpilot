import re
from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.parse import quote

import httpx

from postpilot.channels.base import (
    ChannelError,
    ChannelHeld,
    TransientChannelError,
    UncertainPublication,
)
from postpilot.core.models import Item, Variant

_API = "https://graph.threads.com/v1.0"
_MAX_LENGTH = 500
_TIMESTAMP_MARGIN = timedelta(minutes=5)
_SENTENCE_END = re.compile(r"""[.!?…]+["'”’»“‘)\]]*(?=\s|$)""")


class ThreadsChannel:
    name = "threads"
    token_provider = "threads"
    recover_only = True

    def __init__(
        self, token_source: Callable[[], str | None], client: httpx.Client
    ) -> None:
        self._token_source = token_source
        self._client = client

    def _text(self, item: Item, variants: list[Variant]) -> str:
        for variant in variants:
            if variant.item_id == item.id and variant.lang == "de":
                text = variant.text
                break
        else:
            raise ChannelError("Missing required language variant")
        if not text.strip():
            raise ChannelError("Threads text is empty")
        if len(text) > _MAX_LENGTH:
            budget = _MAX_LENGTH - 1
            sentence_end = max(
                (m.end() for m in _SENTENCE_END.finditer(text) if m.end() <= budget),
                default=0,
            )
            word_end = max(
                (m.end() for m in re.finditer(r"\S+(?=\s)", text) if m.end() <= budget),
                default=budget,
            )
            text = text[: sentence_end or word_end] + "…"
        return text

    def validate(self, item: Item, variants: list[Variant]) -> None:
        self._text(item, variants)

    def _token(self) -> str:
        token = self._token_source()
        if not token:
            raise ChannelHeld("Threads token is unavailable")
        return token

    def _request(
        self,
        method: str,
        endpoint: str,
        token: str,
        *,
        params: dict[str, str | int] | None = None,
        data: dict[str, str] | None = None,
    ) -> dict[str, object]:
        publishing = endpoint == "me/threads_publish"
        transient = UncertainPublication if publishing else TransientChannelError
        try:
            response = self._client.request(
                method,
                f"{_API}/{endpoint}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                data=data,
                timeout=30,
                follow_redirects=False,
            )
        except (httpx.RequestError, httpx.StreamError):
            pass
        except (ValueError, httpx.InvalidURL):
            raise ChannelError("Invalid Threads request") from None
        else:
            if response.status_code in {401, 403}:
                raise ChannelError("Threads authorization failed")
            if response.status_code == 429:
                raise TransientChannelError("Threads rate limit reached")
            if response.status_code >= 500:
                raise transient("Threads service unavailable")
            if not response.is_success:
                raise ChannelError("Threads request rejected")
            try:
                result = response.json()
            except ValueError:
                pass
            else:
                if isinstance(result, dict):
                    return result
            if method == "POST":
                raise transient("Invalid Threads response")
            raise ChannelError("Invalid Threads lookup response")
        raise transient("Threads request failed")

    def publish_with_ref(
        self,
        item: Item,
        variants: list[Variant],
        *,
        ref: str | None,
        claimed_at: datetime,
        save_ref: Callable[[str], None],
    ) -> str | None:
        text = self._text(item, variants)
        token = self._token()
        if ref is not None:
            status = self._request(
                "GET", quote(ref, safe=""), token, params={"fields": "status"}
            ).get("status")
            if status == "PUBLISHED":
                return self._find_published(text, claimed_at, token)
            if status == "IN_PROGRESS":
                return None
            if status in ("ERROR", "EXPIRED"):
                ref = None
            elif status != "FINISHED":
                raise ChannelError("Invalid Threads container status")

        self._check_token(token)
        if ref is None:
            creation_id = self._request(
                "POST", "me/threads", token, data={"media_type": "TEXT", "text": text}
            ).get("id")
            if not isinstance(creation_id, str) or not creation_id:
                raise TransientChannelError("Threads did not return a container ID")
            ref = creation_id

        # Re-saving a FINISHED container also verifies ownership of the current claim.
        save_ref(ref)
        self._check_token(token)
        remote_id = self._request(
            "POST", "me/threads_publish", token, data={"creation_id": ref}
        ).get("id")
        if not isinstance(remote_id, str) or not remote_id:
            raise UncertainPublication("Threads did not return a post ID")
        return remote_id

    def _check_token(self, token: str) -> None:
        if self._token() != token:
            raise ChannelHeld("Threads token changed during the operation")

    def _find_published(
        self, text: str, claimed_at: datetime, token: str
    ) -> str | None:
        params: dict[str, str | int] = {"fields": "id,text,timestamp", "limit": 100}
        cutoff = claimed_at - _TIMESTAMP_MARGIN
        seen = set()
        match = None
        while True:
            self._check_token(token)
            result = self._request("GET", "me/threads", token, params=params)
            posts = result.get("data")
            if not isinstance(posts, list):
                raise ChannelError("Invalid Threads posts response")
            for post in posts:
                if not isinstance(post, dict):
                    raise ChannelError("Invalid Threads post response")
                timestamp = post.get("timestamp")
                if not isinstance(timestamp, str):
                    raise ChannelError("Threads post timestamp is unavailable")
                try:
                    posted_at = datetime.fromisoformat(timestamp)
                except ValueError:
                    raise ChannelError("Invalid Threads post timestamp") from None
                if posted_at.utcoffset() is None:
                    raise ChannelError("Invalid Threads post timestamp")
                if posted_at < cutoff:
                    return match
                if post.get("text") == text:
                    remote_id = post.get("id")
                    if not isinstance(remote_id, str) or not remote_id:
                        raise ChannelError("Threads post ID is unavailable")
                    if match is not None and match != remote_id:
                        return None
                    match = remote_id
            paging = result.get("paging")
            cursors = paging.get("cursors") if isinstance(paging, dict) else None
            after = cursors.get("after") if isinstance(cursors, dict) else None
            if not posts or not isinstance(after, str) or not after:
                return match
            if after in seen:
                raise ChannelError("Threads pagination repeated a cursor")
            seen.add(after)
            params["after"] = after
