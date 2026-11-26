import logging
import traceback
from contextlib import nullcontext
from dataclasses import replace
from datetime import timedelta, timezone
from unittest.mock import Mock, create_autospec
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import Fernet

from postpilot.config import Settings, YouTubeSettings
from postpilot.jobs import tasks
from postpilot.tokens.crypto import TokenCipher
from postpilot.tokens.store import StoredToken, TokenStore
from postpilot.tokens.youtube import (
    REFRESH_TOKEN_STORAGE_EXPIRY,
    YouTubeRefreshError,
    YouTubeTokenManager,
)

REFRESH = "mock-youtube-refresh&secret"
ACCESS = "mock-youtube-access"
CLIENT_SECRET = "mock-youtube-client-secret"
CONFIG = YouTubeSettings("mock-client-id", CLIENT_SECRET)


def test_storage_expiry_leaves_room_for_session_time_zone():
    utc_plus_14 = timezone(timedelta(hours=14))
    assert REFRESH_TOKEN_STORAGE_EXPIRY.astimezone(utc_plus_14).year < 9999


def test_import_encrypts_refresh_token(monkeypatch, caplog):
    key = Fernet.generate_key().decode()
    settings = Settings("db", "bot", 1, key)
    conn = Mock()
    conn.transaction.side_effect = nullcontext
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", REFRESH)
    with caplog.at_level(logging.DEBUG):
        tasks.youtube_import_token(settings, conn)
    provider, access, refresh, expiry = conn.execute.call_args.args[1]
    assert provider == "youtube"
    assert expiry == REFRESH_TOKEN_STORAGE_EXPIRY
    assert TokenCipher(key).decrypt(access) == ""
    assert refresh != REFRESH
    assert TokenCipher(key).decrypt(refresh) == REFRESH
    assert REFRESH not in caplog.text


@pytest.mark.parametrize("value", [None, "", "   "])
def test_import_requires_environment_token(monkeypatch, value):
    monkeypatch.delenv("YOUTUBE_REFRESH_TOKEN", raising=False)
    if value is not None:
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", value)
    with pytest.raises(ValueError, match="^YOUTUBE_REFRESH_TOKEN is required$"):
        tasks.youtube_import_token(Settings("db", "bot", 1, "key"), Mock())


@pytest.mark.parametrize("status", [200, 400, 503])
def test_refresh_is_ephemeral_and_errors_are_sanitized(status, caplog):
    store = create_autospec(TokenStore, instance=True)
    store.get.return_value = StoredToken(
        "youtube", "", REFRESH_TOKEN_STORAGE_EXPIRY, REFRESH
    )

    def respond(request):
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        assert request.method == "POST"
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["refresh_token"],
            "client_id": [CONFIG.client_id],
            "client_secret": [CLIENT_SECRET],
            "refresh_token": [REFRESH],
        }
        return httpx.Response(
            status,
            json={"access_token": ACCESS, "expires_in": 3600}
            if status == 200
            else {"error": "invalid_grant", "error_description": REFRESH},
        )

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        caplog.at_level(logging.DEBUG),
    ):
        manager = YouTubeTokenManager(CONFIG, store, client)
        if status == 200:
            assert manager.access_token() == ACCESS
            assert manager.access_token() == ACCESS
        else:
            with pytest.raises(YouTubeRefreshError) as error:
                manager.access_token()
            assert REFRESH not in "".join(traceback.format_exception(error.value))
    store.save.assert_not_called()
    assert all(v not in caplog.text for v in (REFRESH, ACCESS, CLIENT_SECRET))


@pytest.mark.parametrize("status", [200, 400, 503, None])
def test_check_tokens_refreshes_and_alerts(monkeypatch, status, caplog):
    settings = Settings("db", "bot", 1, "key", youtube=CONFIG)
    store = Mock()
    store.get.return_value = (
        StoredToken("youtube", "", REFRESH_TOKEN_STORAGE_EXPIRY, REFRESH)
        if status is not None
        else None
    )
    state = Mock()
    state.send_token_alert_once.side_effect = lambda provider, day, send: send()
    notifier = Mock()
    monkeypatch.setattr(tasks, "TokenStore", Mock(return_value=store))
    monkeypatch.setattr(tasks, "TokenCipher", Mock())
    monkeypatch.setattr(tasks, "ItemRepo", Mock(return_value=Mock(bot_state=state)))
    monkeypatch.setattr(tasks, "Notifier", Mock(return_value=notifier))
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status,
            json={"access_token": ACCESS}
            if status == 200
            else {"error": "invalid_grant", "error_description": REFRESH},
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(tasks.httpx, "Client", lambda: client)
    with caplog.at_level(logging.DEBUG):
        tasks.check_tokens(settings, Mock())
    assert len(requests) == (0 if status is None else 1)
    if status == 200:
        notifier.send_alert.assert_not_called()
    else:
        message = notifier.send_alert.call_args.args[0]
        assert message.startswith("youtube:")
        assert 'docs/runbook.md ("YouTube Shorts setup")' in message
        assert "postpilot youtube-import-token" in message
    store.save.assert_not_called()
    notifier.send_renewal_prompt.assert_not_called()
    assert all(v not in caplog.text for v in (REFRESH, ACCESS, CLIENT_SECRET))
    # A missing optional client config skips the provider entirely.
    tasks.check_tokens(replace(settings, youtube=None), Mock())


@pytest.mark.parametrize("payload", [{}, [], {"access_token": ""}])
def test_malformed_response_rejected(payload):
    store = Mock()
    store.get.return_value = StoredToken(
        "youtube", "", REFRESH_TOKEN_STORAGE_EXPIRY, REFRESH
    )
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
        ) as client,
        pytest.raises(YouTubeRefreshError),
    ):
        YouTubeTokenManager(CONFIG, store, client).access_token()
    store.save.assert_not_called()
