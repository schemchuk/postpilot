import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from postpilot.core.tiktok import TikTokMode


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LinkedInSettings:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    redirect_uri: str = field(repr=False)
    scopes: tuple[str, ...] = field(repr=False)
    version: str | None = None


@dataclass(frozen=True)
class YouTubeSettings:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)


@dataclass(frozen=True)
class TikTokSettings:
    environment: str
    client_key: str = field(repr=False)
    client_secret: str = field(repr=False)
    redirect_uri: str
    refresh_token: str | None = field(default=None, repr=False)
    mode: TikTokMode = "draft"


@dataclass(frozen=True)
class ThreadsSettings:
    app_id: str = field(repr=False)
    app_secret: str = field(repr=False)
    redirect_uri: str = field(repr=False)


@dataclass(frozen=True)
class ShortioSettings:
    api_key: str = field(repr=False)
    domain: str


@dataclass(frozen=True)
class HeyGenSettings:
    api_key: str = field(repr=False)
    avatar_id: str
    voice_id: str
    engine: str = "avatar_iv"


@dataclass(frozen=True)
class FacebookSettings:
    page_id: str
    graph_version: str = "v27.0"

    @property
    def graph_url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_version}"


@dataclass(frozen=True)
class InstagramSettings:
    account_id: str
    graph_version: str = "v27.0"

    @property
    def graph_url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_version}"


@dataclass(frozen=True)
class ImapSettings:
    user: str = field(repr=False)
    password: str = field(repr=False)
    host: str = "imap.gmail.com"


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    telegram_bot_token: str = field(repr=False)
    telegram_chat_id: int = field(repr=False)
    token_encryption_key: str = field(repr=False)
    linkedin: LinkedInSettings | None = None
    anthropic_api_key: str | None = field(default=None, repr=False)
    threads: ThreadsSettings | None = None
    shortio: ShortioSettings | None = None
    github_owner: str = "schemchuk"
    github_token: str | None = field(default=None, repr=False)

    heygen: HeyGenSettings | None = None
    video_monthly_cap: int = 6
    video_min_balance_usd: float = 3.0
    facebook: FacebookSettings | None = None
    instagram: InstagramSettings | None = None
    imap: ImapSettings | None = None
    youtube: YouTubeSettings | None = None
    tiktok: TikTokSettings | None = None

    @classmethod
    def from_env(
        cls, dotenv: Path | None = None, *, require_token_key: bool = True
    ) -> "Settings":
        values = _read_dotenv(dotenv) if dotenv is not None and dotenv.is_file() else {}
        values.update(os.environ)
        database_url = _require(values, "DATABASE_URL")
        bot_token = _require(values, "TELEGRAM_BOT_TOKEN")
        raw_chat_id = _require(values, "TELEGRAM_CHAT_ID")
        try:
            chat_id = int(raw_chat_id)
        except ValueError:
            raise ConfigError("TELEGRAM_CHAT_ID") from None
        key = _require(values, "TOKEN_ENCRYPTION_KEY") if require_token_key else ""
        client_id = values.get("LINKEDIN_CLIENT_ID", "")
        client_secret = values.get("LINKEDIN_CLIENT_SECRET", "")
        redirect_uri = values.get("LINKEDIN_REDIRECT_URI", "")
        scopes = tuple(values.get("LINKEDIN_SCOPES", "").split())
        linkedin = (
            LinkedInSettings(
                client_id,
                client_secret,
                redirect_uri,
                scopes,
                values.get("LINKEDIN_VERSION"),
            )
            if client_id and client_secret and redirect_uri and scopes
            else None
        )
        youtube_id = values.get("YOUTUBE_CLIENT_ID")
        youtube_secret = values.get("YOUTUBE_CLIENT_SECRET")
        tiktok_mode = values.get("TIKTOK_MODE", "draft")
        if tiktok_mode == "direct":
            raise ConfigError("TIKTOK_MODE=direct comes with T29c2")
        if tiktok_mode != "draft":
            raise ConfigError("TIKTOK_MODE must be draft")
        tiktok_env = values.get("TIKTOK_ENV", "sandbox")
        if tiktok_env not in {"sandbox", "production"}:
            raise ConfigError("TIKTOK_ENV must be sandbox or production")
        tiktok_prefix = "TIKTOK_SANDBOX" if tiktok_env == "sandbox" else "TIKTOK"
        tiktok_key = values.get(f"{tiktok_prefix}_CLIENT_KEY")
        tiktok_secret = values.get(f"{tiktok_prefix}_CLIENT_SECRET")
        app_id = values.get("THREADS_APP_ID")
        app_secret = values.get("THREADS_APP_SECRET")
        threads_redirect = values.get("THREADS_REDIRECT_URI") or redirect_uri
        threads = (
            ThreadsSettings(app_id, app_secret, threads_redirect)
            if app_id and app_secret and threads_redirect
            else None
        )
        shortio_key = values.get("SHORTIO_API_KEY")
        shortio_domain = values.get("SHORTIO_DOMAIN")
        heygen_key = values.get("HEYGEN_API_KEY")
        avatar_id = values.get("HEYGEN_AVATAR_ID")
        voice_id = values.get("HEYGEN_VOICE_ID")
        try:
            cap = int(values.get("VIDEO_MONTHLY_CAP") or "6")
            if cap < 0:
                raise ValueError
        except ValueError:
            raise ConfigError("VIDEO_MONTHLY_CAP") from None
        try:
            balance = float(values.get("VIDEO_MIN_BALANCE_USD") or "3.0")
            if not math.isfinite(balance) or balance < 0:
                raise ValueError
        except ValueError:
            raise ConfigError("VIDEO_MIN_BALANCE_USD") from None
        page_id = values.get("FACEBOOK_PAGE_ID")
        account_id = values.get("INSTAGRAM_BUSINESS_ACCOUNT_ID")
        graph_version = values.get("FACEBOOK_GRAPH_VERSION") or "v27.0"
        imap_user, imap_password = values.get("IMAP_USER"), values.get("IMAP_PASSWORD")
        return cls(
            database_url,
            bot_token,
            chat_id,
            key,
            linkedin,
            values.get("ANTHROPIC_API_KEY") or None,
            threads,
            ShortioSettings(shortio_key, shortio_domain)
            if shortio_key and shortio_domain
            else None,
            values.get("GITHUB_OWNER") or "schemchuk",
            values.get("GITHUB_TOKEN") or None,
            HeyGenSettings(
                heygen_key,
                avatar_id,
                voice_id,
                values.get("HEYGEN_ENGINE") or "avatar_iv",
            )
            if heygen_key and avatar_id and voice_id
            else None,
            cap,
            balance,
            FacebookSettings(page_id, graph_version) if page_id else None,
            InstagramSettings(account_id, graph_version) if account_id else None,
            ImapSettings(
                imap_user, imap_password, values.get("IMAP_HOST") or "imap.gmail.com"
            )
            if imap_user and imap_password
            else None,
            YouTubeSettings(youtube_id, youtube_secret)
            if youtube_id and youtube_secret
            else None,
            TikTokSettings(
                tiktok_env,
                tiktok_key,
                tiktok_secret,
                redirect_uri or "https://schemchuk.github.io/postpilot/callback/",
                values.get(f"{tiktok_prefix}_REFRESH_TOKEN"),
                cast(TikTokMode, tiktok_mode),
            )
            if tiktok_key and tiktok_secret
            else None,
        )

    def secret_values(self) -> tuple[str, ...]:
        values: tuple[str, ...] = (
            self.database_url,
            self.telegram_bot_token,
            str(self.telegram_chat_id),
            self.token_encryption_key,
        )
        if self.linkedin is not None:
            values += (
                self.linkedin.client_id,
                self.linkedin.client_secret,
                self.linkedin.redirect_uri,
            )
        if self.youtube is not None:
            values += (self.youtube.client_id, self.youtube.client_secret)
        if self.tiktok is not None:
            values += (self.tiktok.client_key, self.tiktok.client_secret)
            if self.tiktok.refresh_token:
                values += (self.tiktok.refresh_token,)
        if self.threads is not None:
            values += (
                self.threads.app_id,
                self.threads.app_secret,
                self.threads.redirect_uri,
            )
        if self.shortio is not None:
            values += (self.shortio.api_key,)
        if self.github_token is not None:
            values += (self.github_token,)
        if self.heygen is not None:
            values += (self.heygen.api_key,)
        if self.imap is not None:
            values += (self.imap.user, self.imap.password)
        return values


def _require(values: Mapping[str, str], name: str) -> str:
    if not (value := values.get(name)):
        raise ConfigError(name)
    return value


def _read_dotenv(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        quoted = re.fullmatch(r"""(['"])(.*?)\1\s*(?:#.*)?""", value)
        if quoted:
            values[name] = quoted[2]
        else:
            values[name] = re.split(r"^#|\s+#", value, maxsplit=1)[0].rstrip()
    return values
