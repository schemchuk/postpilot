import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LinkedInSettings:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    redirect_uri: str = field(repr=False)
    scopes: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    telegram_bot_token: str = field(repr=False)
    telegram_chat_id: int = field(repr=False)
    token_encryption_key: str = field(repr=False)
    linkedin: LinkedInSettings | None = None
    anthropic_api_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, dotenv: Path | None = None) -> "Settings":
        values = _read_dotenv(dotenv) if dotenv is not None and dotenv.is_file() else {}
        values.update(os.environ)
        database_url = _require(values, "DATABASE_URL")
        bot_token = _require(values, "TELEGRAM_BOT_TOKEN")
        raw_chat_id = _require(values, "TELEGRAM_CHAT_ID")
        try:
            chat_id = int(raw_chat_id)
        except ValueError:
            raise ConfigError("TELEGRAM_CHAT_ID") from None
        key = _require(values, "TOKEN_ENCRYPTION_KEY")
        client_id = values.get("LINKEDIN_CLIENT_ID", "")
        client_secret = values.get("LINKEDIN_CLIENT_SECRET", "")
        redirect_uri = values.get("LINKEDIN_REDIRECT_URI", "")
        scopes = tuple(values.get("LINKEDIN_SCOPES", "").split())
        linkedin = (
            LinkedInSettings(client_id, client_secret, redirect_uri, scopes)
            if client_id and client_secret and redirect_uri and scopes
            else None
        )
        return cls(
            database_url,
            bot_token,
            chat_id,
            key,
            linkedin,
            values.get("ANTHROPIC_API_KEY") or None,
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
