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
    for name in (
        *REQUIRED,
        *LINKEDIN,
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN",
        "THREADS_APP_ID",
        "THREADS_APP_SECRET",
        "THREADS_REDIRECT_URI",
        "SHORTIO_API_KEY",
        "SHORTIO_DOMAIN",
        "GITHUB_OWNER",
        "GITHUB_TOKEN",
        "IMAP_USER",
        "IMAP_PASSWORD",
        "IMAP_HOST",
        "HEYGEN_API_KEY",
        "HEYGEN_AVATAR_ID",
        "HEYGEN_VOICE_ID",
        "HEYGEN_ENGINE",
        "VIDEO_MONTHLY_CAP",
        "VIDEO_MIN_BALANCE_USD",
        "FACEBOOK_PAGE_ID",
        "FACEBOOK_GRAPH_VERSION",
        "INSTAGRAM_BUSINESS_ACCOUNT_ID",
    ):
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


def test_threads_redirect_fallback_and_redaction(monkeypatch):
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("THREADS_APP_ID", "mock-app-id")
    monkeypatch.setenv("THREADS_APP_SECRET", "mock-app-secret")
    assert Settings.from_env().threads is None
    monkeypatch.setenv("LINKEDIN_REDIRECT_URI", LINKEDIN["LINKEDIN_REDIRECT_URI"])
    settings = Settings.from_env()
    assert settings.linkedin is None
    assert settings.threads.redirect_uri == LINKEDIN["LINKEDIN_REDIRECT_URI"]
    assert "mock-app-secret" in settings.secret_values()
    assert "mock-app-secret" not in repr(settings)
    monkeypatch.setenv("THREADS_REDIRECT_URI", "https://threads.example.test/")
    assert Settings.from_env().threads.redirect_uri == "https://threads.example.test/"


def test_optional_traffic_settings(monkeypatch):
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.shortio is None
    assert settings.github_owner == "schemchuk"
    assert settings.github_token is None

    monkeypatch.setenv("SHORTIO_API_KEY", "mock-private-shortio-key")
    assert Settings.from_env().shortio is None
    monkeypatch.setenv("SHORTIO_DOMAIN", "example.short.gy")
    monkeypatch.setenv("GITHUB_OWNER", "another-owner")
    monkeypatch.setenv("GITHUB_TOKEN", "mock-private-github-token")
    settings = Settings.from_env()
    assert settings.shortio.domain == "example.short.gy"
    assert settings.github_owner == "another-owner"
    for secret in ("mock-private-shortio-key", "mock-private-github-token"):
        assert secret in settings.secret_values()
        assert secret not in repr(settings)


def test_optional_video_settings(monkeypatch):
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HEYGEN_API_KEY", "mock-private-heygen-key")
    monkeypatch.setenv("HEYGEN_AVATAR_ID", "mock-look")
    settings = Settings.from_env()
    assert settings.heygen is None
    assert settings.video_monthly_cap == 6
    assert settings.video_min_balance_usd == 3.0

    monkeypatch.setenv("HEYGEN_VOICE_ID", "mock-voice")
    assert Settings.from_env().heygen.engine == "avatar_iv"
    monkeypatch.setenv("HEYGEN_ENGINE", "custom-engine")
    monkeypatch.setenv("VIDEO_MONTHLY_CAP", "8")
    monkeypatch.setenv("VIDEO_MIN_BALANCE_USD", "4.25")
    settings = Settings.from_env()
    assert settings.heygen.engine == "custom-engine"
    assert settings.heygen.avatar_id == "mock-look"
    assert settings.heygen.voice_id == "mock-voice"
    assert settings.video_monthly_cap == 8
    assert settings.video_min_balance_usd == 4.25
    assert settings.heygen.api_key in settings.secret_values()
    assert settings.heygen.api_key not in repr(settings)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("VIDEO_MONTHLY_CAP", "-1"),
        ("VIDEO_MONTHLY_CAP", "2.5"),
        ("VIDEO_MIN_BALANCE_USD", "NaN"),
        ("VIDEO_MIN_BALANCE_USD", "invalid"),
    ],
)
def test_invalid_video_budget(monkeypatch, name, value):
    for key, setting in REQUIRED.items():
        monkeypatch.setenv(key, setting)
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=f"^{name}$"):
        Settings.from_env()


def test_instagram_is_optional_and_shares_the_facebook_graph_version(monkeypatch):
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "")
    assert Settings.from_env().instagram is None

    monkeypatch.setenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "ig")
    settings = Settings.from_env()
    assert settings.facebook is None
    assert settings.instagram.account_id == "ig"
    assert settings.instagram.graph_url == "https://graph.facebook.com/v27.0"

    monkeypatch.setenv("FACEBOOK_GRAPH_VERSION", "v28.0")
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "fb")
    settings = Settings.from_env()
    assert settings.instagram.graph_url == settings.facebook.graph_url
    assert settings.instagram.graph_version == "v28.0"


def test_imap_config_is_optional_and_credentials_are_redacted(monkeypatch):
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("IMAP_USER", "mailbox@example.test")
    assert Settings.from_env().imap is None
    monkeypatch.setenv("IMAP_PASSWORD", "fake-password")
    settings = Settings.from_env()
    assert settings.imap.host == "imap.gmail.com"
    for secret in ("mailbox@example.test", "fake-password"):
        assert secret not in repr(settings)
        assert secret in settings.secret_values()
    monkeypatch.setenv("IMAP_HOST", "imap.example.test")
    assert Settings.from_env().imap.host == "imap.example.test"


@pytest.mark.parametrize("complete", [False, True])
def test_youtube_settings_optional_and_redacted(monkeypatch, complete):
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "youtube-client-value")
    if complete:
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "youtube-secret-value")
    settings = Settings.from_env()
    if complete:
        assert settings.youtube.client_id == "youtube-client-value"
        assert settings.youtube.client_secret == "youtube-secret-value"
        for value in ("youtube-client-value", "youtube-secret-value"):
            assert value not in repr(settings)
            assert value in settings.secret_values()
    else:
        assert settings.youtube is None


@pytest.mark.parametrize(
    ("environment", "prefix"),
    [(None, "TIKTOK_SANDBOX"), ("sandbox", "TIKTOK_SANDBOX"), ("production", "TIKTOK")],
)
def test_tiktok_environment_selects_credentials(monkeypatch, environment, prefix):
    for name, value in (REQUIRED | LINKEDIN).items():
        monkeypatch.setenv(name, value)
    if environment is None:
        monkeypatch.delenv("TIKTOK_ENV", raising=False)
    else:
        monkeypatch.setenv("TIKTOK_ENV", environment)
    for variant in ("TIKTOK", "TIKTOK_SANDBOX"):
        for suffix in ("CLIENT_KEY", "CLIENT_SECRET", "REFRESH_TOKEN"):
            monkeypatch.setenv(f"{variant}_{suffix}", f"mock-{variant}-{suffix}")
    settings = Settings.from_env()
    config = settings.tiktok
    assert config is not None
    assert config.environment == (environment or "sandbox")
    assert config.client_key == f"mock-{prefix}-CLIENT_KEY"
    assert config.client_secret == f"mock-{prefix}-CLIENT_SECRET"
    assert config.refresh_token == f"mock-{prefix}-REFRESH_TOKEN"
    assert config.redirect_uri == LINKEDIN["LINKEDIN_REDIRECT_URI"]
    assert config.refresh_token in settings.secret_values()
    assert config.refresh_token not in repr(settings)
    monkeypatch.delenv(f"{prefix}_CLIENT_SECRET")
    assert Settings.from_env().tiktok is None


@pytest.mark.parametrize("environment", ["", "staging"])
def test_invalid_tiktok_environment(monkeypatch, environment):
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("TIKTOK_ENV", environment)
    with pytest.raises(ConfigError, match="TIKTOK_ENV must be sandbox or production"):
        Settings.from_env()
