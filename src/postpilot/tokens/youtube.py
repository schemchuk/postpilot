from datetime import UTC, datetime

import httpx

from postpilot.config import YouTubeSettings
from postpilot.tokens.store import TokenStore

# Google refresh tokens have no fixed expiry. The schema requires an expiry date.
# Leave room for PostgreSQL to return the date in the session time zone.
REFRESH_TOKEN_STORAGE_EXPIRY = datetime(9000, 1, 1, tzinfo=UTC)


class YouTubeRefreshError(RuntimeError):
    pass


class YouTubeTokenManager:
    provider = "youtube"

    def __init__(
        self, settings: YouTubeSettings, store: TokenStore, client: httpx.Client
    ) -> None:
        self._settings = settings
        self._store = store
        self._client = client

    def access_token(self) -> str:
        token = self._store.get(self.provider)
        if token is None or not token.refresh_token:
            raise YouTubeRefreshError("YouTube refresh token missing")
        try:
            response = self._client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._settings.client_id,
                    "client_secret": self._settings.client_secret,
                    "refresh_token": token.refresh_token,
                },
                timeout=30,
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, ValueError):
            pass
        else:
            if isinstance(payload, dict):
                access = payload.get("access_token")
                if isinstance(access, str) and access.strip():
                    return access
        raise YouTubeRefreshError("YouTube token refresh failed")
