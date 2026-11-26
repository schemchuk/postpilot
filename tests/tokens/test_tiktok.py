from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet

from postpilot import cli
from postpilot.config import Settings, TikTokSettings
from postpilot.jobs import tasks
from postpilot.notify.renewal import RenewalHandler
from postpilot.tokens.crypto import TokenCipher
from postpilot.tokens.oauth_state import make_state
from postpilot.tokens.store import StoredToken
from postpilot.tokens.tiktok import (
    TikTokTokenError,
    TikTokTokenManager,
    TikTokTokenUnavailable,
)

NOW = datetime(2026, 11, 26, tzinfo=UTC)
EXPIRES = NOW + timedelta(days=365)
REFRESH = "mock-tiktok-refresh&secret"
ACCESS = "mock-tiktok-access"
ROTATED = "mock-tiktok-rotated-refresh"
CONFIG = TikTokSettings(
    "sandbox", "mock-key", "mock-secret", "https://example.test/callback/", REFRESH
)
SCOPES = "user.info.basic,video.upload,video.publish"


def grant(refresh=ROTATED):
    return {
        "access_token": ACCESS,
        "expires_in": 86400,
        "refresh_token": refresh,
        "refresh_expires_in": 31536000,
        "open_id": "mock-account",
        "scope": SCOPES,
    }


def assert_token_request(request, **fields):
    assert request.method == "POST"
    assert str(request.url) == "https://open.tiktokapis.com/v2/oauth/token/"
    assert parse_qs(request.content.decode()) == {
        key: [value]
        for key, value in {
            "client_key": CONFIG.client_key,
            "client_secret": CONFIG.client_secret,
            **fields,
        }.items()
    }


def token_store(remaining=90):
    store = Mock()
    store.lock_provider.side_effect = lambda provider: nullcontext()
    store.get.return_value = (
        StoredToken("tiktok", "", NOW + timedelta(days=remaining), REFRESH)
        if remaining is not None
        else None
    )
    return store


@pytest.mark.parametrize("returned_token", [ROTATED, REFRESH])
def test_refresh_writes_back_refresh_expiry_and_token(returned_token):
    store = token_store()

    def respond(request):
        assert_token_request(request, grant_type="refresh_token", refresh_token=REFRESH)
        return httpx.Response(200, json=grant(returned_token))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert (
            TikTokTokenManager(CONFIG, store, client, lambda: NOW).access_token()
            == ACCESS
        )
    store.lock_provider.assert_called_once_with("tiktok")
    store.save.assert_called_once_with("tiktok", "", EXPIRES, returned_token)


@pytest.mark.parametrize(
    ("status", "payload", "exception"),
    [
        (
            400,
            {"error": "invalid_grant", "error_description": REFRESH},
            TikTokTokenUnavailable,
        ),
        (200, {"error": "revoked"}, TikTokTokenUnavailable),
        (503, {"error": "server_error"}, TikTokTokenError),
        (200, {"access_token": ACCESS}, TikTokTokenError),
    ],
)
def test_failure_keeps_old_token(status, payload, exception):
    store = token_store()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, json=payload)
    )
    with httpx.Client(transport=transport) as client, pytest.raises(exception) as error:
        TikTokTokenManager(CONFIG, store, client, lambda: NOW).access_token()
    assert REFRESH not in str(error.value)
    store.save.assert_not_called()


def test_import_command_validates_and_encrypts(monkeypatch, capsys):
    key = Fernet.generate_key().decode()
    settings = Settings("db", "bot", 1, key, tiktok=CONFIG)
    conn = Mock()
    conn.transaction.side_effect = nullcontext
    monkeypatch.setattr(cli.Settings, "from_env", Mock(return_value=settings))
    monkeypatch.setattr(cli, "connect", lambda _: nullcontext(conn))
    monkeypatch.setattr(cli.migrate, "up", Mock())
    monkeypatch.setattr(cli, "run_job", lambda name, job, config: job() or 0)
    monkeypatch.setattr(tasks, "_now", lambda: NOW)

    def respond(request):
        assert_token_request(request, grant_type="refresh_token", refresh_token=REFRESH)
        return httpx.Response(200, json=grant())

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(tasks.httpx, "Client", lambda: client)
    assert cli.main(["tiktok-import-token"]) == 0
    provider, access, refresh, expiry = conn.execute.call_args.args[1]
    assert provider == "tiktok"
    assert expiry == EXPIRES
    assert TokenCipher(key).decrypt(access) == ""
    assert refresh != ROTATED
    assert TokenCipher(key).decrypt(refresh) == ROTATED
    output = capsys.readouterr().out
    assert (
        output == f"open_id=mock-account, scope={SCOPES}, refresh expiry=2027-11-26\n"
    )


def test_renewal_state_and_exchange():
    store = token_store()
    requests = []

    def respond(request):
        requests.append(request)
        assert_token_request(
            request,
            grant_type="authorization_code",
            code="mock-code",
            redirect_uri=CONFIG.redirect_uri,
        )
        return httpx.Response(200, json=grant())

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        manager = TikTokTokenManager(CONFIG, store, client, lambda: NOW)
        api, nonces, resumed = Mock(), Mock(), Mock()
        active_nonces = set()
        nonces.save_oauth_nonce.side_effect = active_nonces.add
        nonces.consume_oauth_nonce.side_effect = lambda nonce: (
            nonce in active_nonces and active_nonces.remove(nonce) is None
        )
        key = b"mock-key"
        handler = RenewalHandler(
            {"tiktok": manager},
            key,
            api,
            resumed,
            state_store=nonces,
            clock=lambda: NOW,
        )
        assert handler.handle_message(1, 2, "/renew tiktok")
        button = api.send_message.call_args.args[2]["inline_keyboard"][0][0]
        assert button["text"] == "Renew TikTok token"
        url = urlsplit(button["url"])
        assert (url.scheme, url.netloc, url.path) == (
            "https",
            "www.tiktok.com",
            "/v2/auth/authorize/",
        )
        params = parse_qs(url.query)
        (state,) = params.pop("state")
        assert state.startswith("tiktok.")
        assert params == {
            "client_key": [CONFIG.client_key],
            "scope": [SCOPES],
            "response_type": ["code"],
            "redirect_uri": [CONFIG.redirect_uri],
        }

        foreign = make_state(key, "threads", NOW, store=nonces)
        for invalid in (foreign, state.replace("tiktok.", "threads.", 1)):
            assert handler.handle_message(1, 2, f"/renew mock-code {invalid}")
            assert "renewal failed" in api.send_message.call_args.args[1]
        assert not requests
        store.save.assert_not_called()

        command = f"/renew mock-code {state}"
        assert handler.handle_message(1, 2, command)
        assert (
            api.send_message.call_args.args[1]
            == "TikTok token renewed until 2027-11-26"
        )
        assert handler.handle_message(1, 2, command)
        assert "renewal failed" in api.send_message.call_args.args[1]
    assert len(requests) == 1
    resumed.assert_called_once_with("tiktok")
    store.save.assert_called_once_with("tiktok", "", EXPIRES, ROTATED)


@pytest.mark.parametrize(
    ("remaining", "error", "alert"),
    [
        (29, None, "refresh token expires in 29 days"),
        (30, None, "refresh token expires in 30 days"),
        (31, None, None),
        (31, "invalid_grant", "consent missing or revoked, items will be held"),
        (31, "server_error", "token refresh failed"),
        (None, None, None),
    ],
)
def test_check_tokens(monkeypatch, remaining, error, alert):
    store, state, notifier = token_store(remaining), Mock(), Mock()
    state.send_token_alert_once.side_effect = lambda provider, day, send: send()
    state.send_renewal_prompt_once.side_effect = lambda provider, day, send: send()
    monkeypatch.setattr(tasks, "TokenStore", Mock(return_value=store))
    monkeypatch.setattr(tasks, "TokenCipher", Mock())
    monkeypatch.setattr(tasks, "ItemRepo", Mock(return_value=Mock(bot_state=state)))
    monkeypatch.setattr(tasks, "Notifier", Mock(return_value=notifier))
    monkeypatch.setattr(tasks, "_now", lambda: NOW)
    response = (
        httpx.Response(400, json={"error": error})
        if error
        else httpx.Response(200, json=grant())
    )
    respond = Mock(return_value=response)
    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(tasks.httpx, "Client", lambda: client)

    tasks.check_tokens(Settings("db", "bot", 1, "key", tiktok=CONFIG), Mock())
    assert respond.call_count == (0 if remaining is None else 1)
    if alert:
        notifier.send_alert.assert_called_once_with(f"tiktok: {alert}")
        notifier.send_renewal_prompt.assert_called_once()
    else:
        notifier.send_alert.assert_not_called()
        notifier.send_renewal_prompt.assert_not_called()
