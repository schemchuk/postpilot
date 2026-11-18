from dataclasses import FrozenInstanceError

import pytest

from postpilot.config import ConfigError, Settings

REQUIRED = {
    "DATABASE_URL": "database-private-value",
    "TELEGRAM_BOT_TOKEN": "telegram-private-value",
    "TELEGRAM_CHAT_ID": "987654321",
    "TOKEN_ENCRYPTION_KEY": "encryption-private-value",
}
LINKEDIN = {
    "LINKEDIN_CLIENT_ID": "linkedin-client-value",
    "LINKEDIN_CLIENT_SECRET": "linkedin-private-value",
    "LINKEDIN_REDIRECT_URI": "https://callback.example.test/",
    "LINKEDIN_SCOPES": "openid  profile",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in REQUIRED | LINKEDIN:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("missing", REQUIRED)
def test_missing_reports_variable_only(monkeypatch, missing):
    for name, value in REQUIRED.items():
        if name != missing:
            monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError) as error:
        Settings.from_env()
    assert str(error.value) == missing


def test_settings_repr_and_mutability(monkeypatch):
    for name, value in (REQUIRED | LINKEDIN).items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.telegram_chat_id == 987654321
    assert settings.linkedin.scopes == ("openid", "profile")
    for value in (REQUIRED | LINKEDIN).values():
        assert value not in repr(settings)
    with pytest.raises(FrozenInstanceError):
        settings.telegram_chat_id = 1
    monkeypatch.delenv("LINKEDIN_CLIENT_SECRET")
    assert Settings.from_env().linkedin is None


def test_dotenv(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        "# Local test file\n"
        "DATABASE_URL=fake-url?a=b&c=d # comment\n"
        "TELEGRAM_BOT_TOKEN='value=a&b#keep' # comment\n"
        "TELEGRAM_CHAT_ID=987654321\n"
        'TOKEN_ENCRYPTION_KEY="file-key==&more"\n'
        "unrecognized line\n"
    )
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "env-key")
    settings = Settings.from_env(path)
    assert settings.database_url == "fake-url?a=b&c=d"
    assert settings.telegram_bot_token == "value=a&b#keep"
    assert settings.token_encryption_key == "env-key"
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "invalid-private-value")
    with pytest.raises(ConfigError, match="^TELEGRAM_CHAT_ID$"):
        Settings.from_env(path)
