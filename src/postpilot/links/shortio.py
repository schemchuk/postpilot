import logging
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from psycopg import Error

from postpilot.config import ShortioSettings
from postpilot.links.store import LinkStore, ShortLink

logger = logging.getLogger(__name__)
_CREATE_URL = "https://api.short.io/links"
_STATS_URL = "https://api-v2.short.io/statistics/link"


class ShortioClient:
    def __init__(
        self,
        settings: ShortioSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._domain = settings.domain
        self._client = httpx.Client(
            transport=transport,
            headers={"Accept": "application/json", "Authorization": settings.api_key},
        )

    def close(self) -> None:
        self._client.close()

    def create_link(self, destination: str, repo: str, lang: str) -> str | None:
        link = self.create_record(destination, repo, lang)
        return link.short_url if link is not None else None

    def create_record(
        self, destination: str, repo: str, lang: str, *, item_id: UUID | None = None
    ) -> ShortLink | None:
        day = datetime.now(UTC).date()
        try:
            response = self._client.post(
                _CREATE_URL,
                json={
                    "originalURL": destination,
                    "domain": self._domain,
                    "allowDuplicates": True,
                    "title": f"{repo} — {lang} — {day}",
                },
            )
            response.raise_for_status()
            data = response.json()
            link_id = data.get("idString") or data.get("id")
            short_url = data["shortURL"]
            if type(link_id) not in (str, int) or not link_id:
                raise ValueError
            url = urlsplit(short_url)
            if url.scheme not in ("https", "http") or not url.hostname:
                raise ValueError
            return ShortLink(
                str(link_id), item_id, repo, lang, short_url, destination, day
            )
        except Exception:  # noqa: BLE001 - Tracking must fail open.
            logger.warning("Short.io link creation failed")
            return None

    def total_clicks(self, link_id: str) -> int | None:
        try:
            response = self._client.get(
                f"{_STATS_URL}/{quote(link_id, safe='')}",
                params={"period": "total", "tzOffset": 0},
            )
            response.raise_for_status()
            clicks = response.json()["totalClicks"]
            if type(clicks) is not int or clicks < 0:
                raise ValueError
            return clicks
        except Exception:  # noqa: BLE001 - Tracking must fail open.
            logger.warning("Short.io statistics request failed")
            return None


def tracked_link(
    client: ShortioClient | None,
    store: LinkStore,
    destination: str,
    repo: str,
    lang: str,
    *,
    item_id: UUID | None = None,
    strict_storage: bool = False,
) -> str:
    if client is not None:
        link = client.create_record(destination, repo, lang, item_id=item_id)
        if link is not None:
            try:
                store.save_link(link)
                return link.short_url
            except Error:
                if strict_storage:
                    raise
                logger.warning("Short link storage failed")
    return destination
