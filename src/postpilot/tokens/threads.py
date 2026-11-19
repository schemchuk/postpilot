import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from postpilot.tokens.base import days_left, utc_datetime
from postpilot.tokens.store import TokenStore

_GRAPH = "https://graph.threads.com"
_SCOPES = (
    "threads_basic,threads_content_publish,threads_read_replies,threads_manage_replies"
)
TOKEN_LIFETIME = timedelta(days=60)


def _exclude_token_requests(record: logging.LogRecord) -> bool:
    # HTTPX logs full URLs, including the credentials required by these endpoints.
    return not any(
        f"{_GRAPH}/{path}?" in record.getMessage()
        for path in ("access_token", "refresh_access_token")
    )


logging.getLogger("httpx").addFilter(_exclude_token_requests)


class ThreadsRenewalError(RuntimeError):
    pass


class ThreadsTokenManager:
    provider = "threads"
    refresh_under_days = 14

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        redirect_uri: str,
        store: TokenStore,
        client: httpx.Client,
        clock: Callable[[], datetime],
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._redirect_uri = redirect_uri
        self._store = store
        self._client = client
        self._clock = clock

    def days_left(self, now: datetime) -> float | None:
        token = self._store.get(self.provider)
        return days_left(token.expires_at, now) if token is not None else None

    def renewal_url(self, state: str) -> str:
        query = urlencode(
            {
                "client_id": self._app_id,
                "redirect_uri": self._redirect_uri,
                "scope": _SCOPES,
                "response_type": "code",
                "state": state,
            }
        )
        return f"https://threads.net/oauth/authorize?{query}"

    def try_refresh(self) -> bool:
        token = self._store.get(self.provider)
        now = utc_datetime(self._clock())
        if (
            token is None
            or token.expires_at <= now
            or token.updated_at is None
            or now - token.updated_at < timedelta(days=1)
        ):
            return False
        self._save(
            "refresh_access_token",
            grant_type="th_refresh_token",
            access_token=token.access_token,
        )
        return True

    def complete_renewal(self, code: str) -> datetime:
        access, _ = self._exchange(
            "POST",
            "oauth/access_token",
            client_id=self._app_id,
            client_secret=self._app_secret,
            grant_type="authorization_code",
            redirect_uri=self._redirect_uri,
            code=code,
        )
        return self._save(
            "access_token",
            grant_type="th_exchange_token",
            client_secret=self._app_secret,
            access_token=access,
        )

    def _save(self, endpoint: str, **params: str) -> datetime:
        access, seconds = self._exchange("GET", endpoint, **params)
        if type(seconds) is not int or seconds <= 0:
            raise ThreadsRenewalError("Invalid Threads token expiry")
        try:
            expires_at = utc_datetime(self._clock()) + timedelta(seconds=seconds)
        except (OverflowError, ValueError):
            raise ThreadsRenewalError("Invalid Threads token expiry") from None
        self._store.save(self.provider, access, expires_at)
        return expires_at

    def _exchange(
        self, method: str, endpoint: str, **fields: str
    ) -> tuple[str, object]:
        try:
            response = self._client.request(
                method,
                f"{_GRAPH}/{endpoint}",
                params=fields if method == "GET" else None,
                data=fields if method == "POST" else None,
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
                if isinstance(access, str) and access:
                    return access, payload.get("expires_in")
        raise ThreadsRenewalError("Threads token exchange failed")
