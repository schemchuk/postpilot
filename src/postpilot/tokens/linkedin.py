from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from postpilot.tokens.base import days_left, utc_datetime
from postpilot.tokens.store import TokenStore

_OAUTH_URL = "https://www.linkedin.com/oauth/v2"


class RenewalError(RuntimeError):
    pass


class LinkedInTokenManager:
    provider = "linkedin"
    refresh_under_days = 7

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: list[str],
        store: TokenStore,
        client: httpx.Client,
        clock: Callable[[], datetime],
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._scopes = tuple(scopes)
        self._store = store
        self._client = client
        self._clock = clock

    def days_left(self, now: datetime) -> float | None:
        token = self._store.get(self.provider)
        return days_left(token.expires_at, now) if token is not None else None

    def renewal_url(self, state: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "state": state,
                "scope": " ".join(self._scopes),
            }
        )
        return f"{_OAUTH_URL}/authorization?{query}"

    def complete_renewal(self, code: str) -> datetime:
        return self._exchange(
            grant_type="authorization_code", code=code, redirect_uri=self._redirect_uri
        )

    def try_refresh(self) -> bool:
        token = self._store.get(self.provider)
        if token is None or not token.refresh_token:
            return False
        self._exchange(grant_type="refresh_token", refresh_token=token.refresh_token)
        return True

    def _exchange(self, **form: str) -> datetime:
        try:
            response = self._client.post(
                f"{_OAUTH_URL}/accessToken",
                data={
                    **form,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError
            access = payload.get("access_token")
            refresh = payload.get("refresh_token", form.get("refresh_token"))
            seconds = payload.get("expires_in")
            if not isinstance(access, str) or not access:
                raise ValueError
            if refresh is not None and (not isinstance(refresh, str) or not refresh):
                raise ValueError
            if type(seconds) is not int or seconds <= 0:
                raise ValueError
            expires_at = utc_datetime(self._clock()) + timedelta(seconds=seconds)
        except (
            TypeError,
            httpx.HTTPError,
            httpx.InvalidURL,
            httpx.StreamError,
            ValueError,
            OverflowError,
        ):
            pass
        else:
            self._store.save(self.provider, access, expires_at, refresh)
            return expires_at
        raise RenewalError("LinkedIn token exchange failed")
