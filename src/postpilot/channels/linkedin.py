import os
import re
from collections.abc import Callable, Iterator
from hashlib import sha256
from uuid import UUID

import httpx

from postpilot.channels.base import (
    ChannelError,
    ChannelHeld,
    TransientChannelError,
    UncertainPublication,
    marker,
)
from postpilot.core.models import Item, Language, Variant

LINKEDIN_TEXT_CHANNELS: dict[str, Language] = {"linkedin": "de", "linkedin-ua": "ua"}

_POSTS = "https://api.linkedin.com/rest/posts"
_VIDEOS = "https://api.linkedin.com/rest/videos"
_IMAGES = "https://api.linkedin.com/rest/images"
_USERINFO = "https://api.linkedin.com/v2/userinfo"
_MAX_LENGTH = 3000
# Keep hashtag and @mention prefixes. Preserve explicit LinkedIn URN mentions.
_LITTLE_TEXT = re.compile(
    r"(@\[[^\[\]\r\n]+\]\(urn:li:(?:person|organization):[\w-]+\))|([\\|{}\[\]()<>*_~])"
)


class LinkedInChannel:
    token_provider = "linkedin"
    recover_only = True

    def __init__(
        self,
        name: str,
        lang: Language,
        token_source: Callable[[], str | None],
        client: httpx.Client,
        version: str | None = None,
        limit: int = _MAX_LENGTH,
        *,
        video_marker: bool = False,
    ) -> None:
        self.name = name
        self._lang = lang
        self._token_source = token_source
        self._client = client
        self._version = version or os.getenv("LINKEDIN_VERSION") or "202607"
        self._limit = limit
        self._marker_tail = "/video" if video_marker else ""
        self._identity: tuple[bytes, str] | None = None

    def _suffix(self, item_id: UUID) -> str:
        return f"\n\n{marker(item_id)}/{self._lang}{self._marker_tail}"

    def _commentary(
        self,
        item: Item,
        variants: list[Variant],
        *,
        label: str = "",
        leading_label: str = "",
        include_marker: bool = True,
    ) -> str:
        for variant in variants:
            if variant.item_id == item.id and variant.lang == self._lang:
                text = variant.text
                break
        else:
            raise ChannelError("Missing required language variant")
        if not text.strip():
            raise ChannelError("LinkedIn text is empty")
        suffix = self._suffix(item.id) if include_marker else ""
        if leading_label:
            text = leading_label + "\n\n" + text
        if label:
            text += "\n\n" + label
        if len(text) > self._limit or len(text) + len(suffix) > _MAX_LENGTH:
            raise ChannelError("Text exceeds the LinkedIn length limit")
        text = _LITTLE_TEXT.sub(lambda match: match[1] or "\\" + match[2], text)
        return text + suffix

    def validate(self, item: Item, variants: list[Variant]) -> None:
        self._commentary(item, variants)

    def _token(self) -> str:
        token = self._token_source()
        if not token:
            raise ChannelHeld("LinkedIn token is unavailable")
        return token

    def _same_token(self, token: str) -> str:
        if self._token() != token:
            raise ChannelHeld("LinkedIn token changed during the operation")
        return token

    def _request(
        self,
        method: str,
        url: str,
        token: str,
        *,
        params: dict[str, str | int] | None = None,
        payload: dict[str, object] | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"}
        if (
            url == _POSTS
            or url in {_VIDEOS, _IMAGES}
            or url.startswith((_VIDEOS + "/", _IMAGES + "/"))
        ):
            headers.update(
                {
                    "LinkedIn-Version": self._version,
                    "X-Restli-Protocol-Version": "2.0.0",
                }
            )
        transient: type[ChannelError] = (
            UncertainPublication
            if method == "POST" and url == _POSTS
            else TransientChannelError
        )
        try:
            response = self._client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=payload,
                timeout=30,
                follow_redirects=False,
            )
        except (httpx.RequestError, httpx.StreamError):
            pass
        except (ValueError, httpx.InvalidURL):
            transient = ChannelError
        else:
            if response.is_success or (
                method == "GET"
                and url.startswith(_IMAGES + "/")
                and response.status_code == 404
            ):
                return response
            if response.status_code in {401, 403}:
                raise ChannelError("LinkedIn authorization failed")
            if response.status_code == 429:
                raise TransientChannelError("LinkedIn rate limit reached")
            if response.status_code >= 500:
                raise transient("LinkedIn service unavailable")
            raise ChannelError("LinkedIn request rejected")
        raise transient("LinkedIn request failed")

    @staticmethod
    def _object(response: httpx.Response) -> dict[str, object]:
        try:
            result = response.json()
        except ValueError:
            pass
        else:
            if isinstance(result, dict):
                return result
        raise ChannelError("Invalid LinkedIn response")

    def _author(self, token: str) -> str:
        fingerprint = sha256(token.encode()).digest()
        if self._identity is None or self._identity[0] != fingerprint:
            subject = self._object(self._request("GET", _USERINFO, token)).get("sub")
            if not isinstance(subject, str) or not subject:
                raise ChannelError("LinkedIn user identity is unavailable")
            self._identity = (fingerprint, f"urn:li:person:{subject}")
        return self._identity[1]

    def publish(self, item: Item, variants: list[Variant]) -> str:
        commentary = self._commentary(item, variants)
        token = self._token()
        author = self._author(token)
        return self._create_post(commentary, author, token)

    def _create_post(
        self, commentary: str, author: str, token: str, *, media: str | None = None
    ) -> str:
        payload: dict[str, object] = {
            "author": author,
            "commentary": commentary,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if media is not None:
            payload["content"] = {"media": {"id": media}}
        response = self._request(
            "POST", _POSTS, self._same_token(token), payload=payload
        )
        remote_id = response.headers.get("x-restli-id")
        if not isinstance(remote_id, str) or not remote_id:
            raise UncertainPublication("LinkedIn did not return a post ID")
        return remote_id

    def find_existing(self, item_id: UUID) -> str | None:
        return self._find_existing(item_id, self._token())

    def _find_existing(
        self, item_id: UUID, token: str, *, video: str | None = None
    ) -> str | None:
        suffix = self._suffix(item_id)
        for post in self._author_posts(token):
            commentary = post.get("commentary")
            content = post.get("content")
            media = content.get("media") if isinstance(content, dict) else None
            matches = (
                isinstance(media, dict) and media.get("id") == video
                if video is not None
                else isinstance(commentary, str) and commentary.endswith(suffix)
            )
            if matches:
                return self._post_id(post)
        return None

    def _find_text_post(
        self, text: str, token: str, since_ms: int, *, media: str | None = None
    ) -> str | None:
        match = None
        for post in self._author_posts(token):
            published_at = post.get("publishedAt")
            content = post.get("content")
            if media is None:
                matches_media = content is None
            else:
                attachment = content.get("media") if isinstance(content, dict) else None
                matches_media = (
                    isinstance(attachment, dict) and attachment.get("id") == media
                )
            if (
                post.get("commentary") != text
                or not matches_media
                or type(published_at) is not int
                or published_at < since_ms
            ):
                continue
            found_id = self._post_id(post)
            if match is not None and match != found_id:
                return None
            match = found_id
        return match

    @staticmethod
    def _post_id(post: dict[str, object]) -> str:
        found_id = post.get("id")
        if not isinstance(found_id, str) or not found_id:
            raise ChannelError("LinkedIn post ID is unavailable")
        return found_id

    def _author_posts(self, token: str) -> Iterator[dict[str, object]]:
        author = self._author(token)
        start = 0
        while True:
            response = self._request(
                "GET",
                _POSTS,
                self._same_token(token),
                params={
                    "q": "author",
                    "author": author,
                    "count": 100,
                    "start": start,
                },
            )
            result = self._object(response)
            elements = result.get("elements")
            if not isinstance(elements, list):
                raise ChannelError("Invalid LinkedIn posts response")
            for post in elements:
                if not isinstance(post, dict):
                    raise ChannelError("Invalid LinkedIn post response")
                yield post
            if not elements:
                return
            paging = result.get("paging")
            links = paging.get("links") if isinstance(paging, dict) else None
            if isinstance(links, list) and not any(
                isinstance(link, dict) and link.get("rel") == "next" for link in links
            ):
                return
            if links is None and len(elements) < 100:
                return
            start += len(elements)
