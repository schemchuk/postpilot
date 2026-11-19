import re
from collections.abc import Callable
from uuid import UUID

import httpx

from postpilot.channels.base import (
    ChannelError,
    ChannelHeld,
    TransientChannelError,
    UncertainPublication,
    marker,
)
from postpilot.core.models import Item, Variant

_API = "https://graph.threads.com/v1.0/me"
_MAX_LENGTH = 500
_SENTENCE_END = re.compile(r"""[.!?…]+["'”’»“‘)\]]*(?=\s|$)""")


def _suffix(item_id: UUID) -> str:
    return f"\n\n{marker(item_id)}/de"


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
        suffix = _suffix(item.id)
        limit = _MAX_LENGTH - len(suffix)
        if len(text) > limit:
            budget = limit - 1
            sentence_end = max(
                (m.end() for m in _SENTENCE_END.finditer(text) if m.end() <= budget),
                default=0,
            )
            word_end = max(
                (m.end() for m in re.finditer(r"\S+(?=\s)", text) if m.end() <= budget),
                default=budget,
            )
            text = text[: sentence_end or word_end] + "…"
        return text + suffix

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
        transient = UncertainPublication if method == "POST" else TransientChannelError
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
                raise UncertainPublication("Invalid Threads publish response")
            raise ChannelError("Invalid Threads lookup response")
        raise transient("Threads request failed")

    @staticmethod
    def _id(result: dict[str, object]) -> str:
        remote_id = result.get("id")
        if not isinstance(remote_id, str) or not remote_id:
            raise UncertainPublication("Threads did not return an ID")
        return remote_id

    def publish(self, item: Item, variants: list[Variant]) -> str:
        text = self._text(item, variants)
        token = self._token()
        creation_id = self._id(
            self._request(
                "POST", "threads", token, data={"media_type": "TEXT", "text": text}
            )
        )
        if self._token() != token:
            raise ChannelHeld("Threads token changed during the operation")
        return self._id(
            self._request(
                "POST", "threads_publish", token, data={"creation_id": creation_id}
            )
        )

    def find_existing(self, item_id: UUID) -> str | None:
        token = self._token()
        suffix = _suffix(item_id)
        params: dict[str, str | int] = {"fields": "id,text", "limit": 100}
        seen = set()
        while True:
            if self._token() != token:
                raise ChannelHeld("Threads token changed during the operation")
            result = self._request("GET", "threads", token, params=params)
            posts = result.get("data")
            if not isinstance(posts, list):
                raise ChannelError("Invalid Threads posts response")
            for post in posts:
                if not isinstance(post, dict):
                    raise ChannelError("Invalid Threads post response")
                text, remote_id = post.get("text"), post.get("id")
                if isinstance(text, str) and text.endswith(suffix):
                    if not isinstance(remote_id, str) or not remote_id:
                        raise ChannelError("Threads post ID is unavailable")
                    return remote_id
            paging = result.get("paging")
            cursors = paging.get("cursors") if isinstance(paging, dict) else None
            after = cursors.get("after") if isinstance(cursors, dict) else None
            if not posts or not isinstance(after, str) or not after:
                return None
            if after in seen:
                raise ChannelError("Threads pagination repeated a cursor")
            seen.add(after)
            params["after"] = after
