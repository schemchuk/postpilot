import logging
import traceback
from datetime import UTC, datetime, timedelta
from unittest.mock import create_autospec
from urllib.parse import parse_qs

import httpx
import pytest

from postpilot.tokens.store import StoredToken, TokenStore
from postpilot.tokens.threads import ThreadsRenewalError, ThreadsTokenManager

NOW = datetime(2026, 11, 19, tzinfo=UTC)
APP_SECRET = "mock-app-secret"
SHORT_TOKEN = "mock-short-token"
LONG_TOKEN = "mock-long-token"
EXPIRY = NOW + timedelta(days=60)
REDIRECT = "https://example.test/callback/"


@pytest.fixture
def store():
    return create_autospec(TokenStore, instance=True)


def manager(store, client):
    return ThreadsTokenManager(
        "app-id", APP_SECRET, REDIRECT, store, client, lambda: NOW
    )


def test_renewal_exchanges_and_saves_long_lived_token(store, caplog):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.host == "graph.threads.com"
        if request.url.path == "/oauth/access_token":
            assert request.method == "POST"
            assert parse_qs(request.content.decode()) == {
                "client_id": ["app-id"],
                "client_secret": [APP_SECRET],
                "grant_type": ["authorization_code"],
                "redirect_uri": [REDIRECT],
                "code": ["example-code"],
            }
            return httpx.Response(200, json={"access_token": SHORT_TOKEN})
        assert request.url.path == "/access_token"
        assert request.method == "GET"
        assert dict(request.url.params) == {
            "grant_type": "th_exchange_token",
            "client_secret": APP_SECRET,
            "access_token": SHORT_TOKEN,
        }
        return httpx.Response(
            200, json={"access_token": LONG_TOKEN, "expires_in": 60 * 86400}
        )

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        caplog.at_level(logging.DEBUG),
    ):
        threads = manager(store, client)
        url = httpx.URL(threads.renewal_url("signed state"))
        assert url.host == "threads.net"
        assert url.path == "/oauth/authorize"
        assert dict(url.params) == {
            "client_id": "app-id",
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "state": "signed state",
            "scope": "threads_basic,threads_content_publish,threads_read_replies,threads_manage_replies",
        }
        assert threads.complete_renewal("example-code") == EXPIRY
    assert len(requests) == 2
    store.save.assert_called_once_with("threads", LONG_TOKEN, EXPIRY)
    assert all(
        value not in caplog.text for value in (APP_SECRET, SHORT_TOKEN, LONG_TOKEN)
    )


def test_refresh_checks_age_and_saves_new_expiry(store):
    def respond(request):
        assert request.method == "GET"
        assert request.url.path == "/refresh_access_token"
        assert dict(request.url.params) == {
            "grant_type": "th_refresh_token",
            "access_token": LONG_TOKEN,
        }
        return httpx.Response(
            200, json={"access_token": "refreshed-token", "expires_in": 60 * 86400}
        )

    expiry = NOW + timedelta(days=10)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        threads = manager(store, client)
        assert threads.refresh_under_days == 14
        store.get.return_value = StoredToken(
            "threads", LONG_TOKEN, expiry, updated_at=NOW - timedelta(hours=23)
        )
        assert threads.days_left(NOW) == 10
        assert not threads.try_refresh()
        store.save.assert_not_called()
        store.get.return_value = StoredToken(
            "threads", LONG_TOKEN, expiry, updated_at=NOW - timedelta(hours=24)
        )
        assert threads.try_refresh()
    store.save.assert_called_once_with("threads", "refreshed-token", EXPIRY)


def test_refresh_failure_is_sanitized(store, caplog):
    store.get.return_value = StoredToken(
        "threads", LONG_TOKEN, EXPIRY, updated_at=NOW - timedelta(days=2)
    )
    transport = httpx.MockTransport(lambda r: httpx.Response(403, text=LONG_TOKEN))
    with (
        httpx.Client(transport=transport) as client,
        caplog.at_level(logging.DEBUG),
        pytest.raises(ThreadsRenewalError) as exc,
    ):
        manager(store, client).try_refresh()
    assert LONG_TOKEN not in "".join(traceback.format_exception(exc.value))
    assert LONG_TOKEN not in caplog.text
    store.save.assert_not_called()
