from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from postpilot.config import TikTokSettings
from postpilot.tokens.base import days_left, utc_datetime
from postpilot.tokens.store import TokenStore

_SCOPES = "user.info.basic,video.upload,video.publish"


class TikTokTokenError(RuntimeError):
    pass


class TikTokTokenUnavailable(TikTokTokenError):
    """Consent is missing, expired or revoked. Publishing should be held."""


@dataclass(frozen=True)
class TikTokGrant:
    open_id: str
    scope: str
    refresh_expires_at: datetime


class TikTokTokenManager:
    provider = "tiktok"
    refresh_under_days = 30

    def __init__(
        self,
        settings: TikTokSettings,
        store: TokenStore,
        client: httpx.Client,
        clock: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._store = store
        self._client = client
        self._clock = clock

    def days_left(self, now: datetime) -> float | None:
        token = self._store.get(self.provider)
        return days_left(token.expires_at, now) if token else None

    def renewal_url(self, state: str) -> str:
        query = urlencode(
            {
                "client_key": self._settings.client_key,
                "scope": _SCOPES,
                "response_type": "code",
                "redirect_uri": self._settings.redirect_uri,
                "state": state,
            }
        )
        return f"https://www.tiktok.com/v2/auth/authorize/?{query}"

    def complete_renewal(self, code: str) -> datetime:
        with self._store.lock_provider(self.provider):
            _, grant = self._exchange(
                grant_type="authorization_code",
                code=code,
                redirect_uri=self._settings.redirect_uri,
            )
        return grant.refresh_expires_at

    def import_token(self, refresh_token: str) -> TikTokGrant:
        with self._store.lock_provider(self.provider):
            _, grant = self._exchange(
                grant_type="refresh_token", refresh_token=refresh_token
            )
        return grant

    def access_token(self) -> str:
        with self._store.lock_provider(self.provider):
            token = self._store.get(self.provider)
            if (
                not token
                or not token.refresh_token
                or token.expires_at <= self._clock()
            ):
                raise TikTokTokenUnavailable("TikTok refresh token missing or expired")
            access, _ = self._exchange(
                grant_type="refresh_token", refresh_token=token.refresh_token
            )
            return access

    def try_refresh(self) -> bool:
        self.access_token()
        return True

    def _exchange(self, **form: str) -> tuple[str, TikTokGrant]:
        try:
            response = self._client.post(
                "https://open.tiktokapis.com/v2/oauth/token/",
                data={
                    **form,
                    "client_key": self._settings.client_key,
                    "client_secret": self._settings.client_secret,
                },
                timeout=30,
                follow_redirects=False,
            )
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError
            if payload.get("error") in ("invalid_grant", "revoked", "token_revoked"):
                raise TikTokTokenUnavailable("TikTok consent revoked or invalid")
            response.raise_for_status()
            if payload.get("error"):
                raise ValueError
            access, refresh = payload.get("access_token"), payload.get("refresh_token")
            open_id, scope = payload.get("open_id"), payload.get("scope")
            seconds = payload.get("refresh_expires_in")
            if not (
                isinstance(access, str)
                and isinstance(refresh, str)
                and isinstance(open_id, str)
                and isinstance(scope, str)
                and all(value.strip() for value in (access, refresh, open_id, scope))
            ):
                raise ValueError
            if type(seconds) is not int or seconds <= 0:
                raise ValueError
            expiry = utc_datetime(self._clock()) + timedelta(seconds=seconds)
        except (
            httpx.HTTPError,
            httpx.InvalidURL,
            httpx.StreamError,
            ValueError,
            OverflowError,
            TypeError,
        ):
            raise TikTokTokenError("TikTok token exchange failed") from None
        # expires_at tracks refresh expiry. The access token is used only in this run.
        self._store.save(self.provider, "", expiry, refresh)
        return access, TikTokGrant(open_id, scope, expiry)
