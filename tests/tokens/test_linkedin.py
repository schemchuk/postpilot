from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from postpilot.tokens.linkedin import LinkedInTokenManager, RenewalError
from postpilot.tokens.store import StoredToken, TokenStore

NOW = datetime(2026, 11, 18, tzinfo=UTC)
REDIRECT = "https://example.test/callback/"
CLIENT_SECRET = "test client credential"
CODE = "test authorization code"


@pytest.fixture
def setup_manager():
    requests = []
    store = Mock(spec=TokenStore)
    store.get.return_value = None

    def setup(response):
        def transport(request):
            requests.append(request)
            return response

        client = httpx.Client(transport=httpx.MockTransport(transport))
        return LinkedInTokenManager(
            "test-id",
            CLIENT_SECRET,
            REDIRECT,
            ["openid", "w_member_social"],
            store,
            client,
            lambda: NOW,
        )

    return setup, store, requests


def test_authorization_url(setup_manager):
    setup, _, requests = setup_manager
    url = urlsplit(setup(None).renewal_url("state+value"))
    assert (url.scheme, url.netloc, url.path) == (
        "https",
        "www.linkedin.com",
        "/oauth/v2/authorization",
    )
    assert parse_qs(url.query) == {
        "response_type": ["code"],
        "client_id": ["test-id"],
        "redirect_uri": [REDIRECT],
        "state": ["state+value"],
        "scope": ["openid w_member_social"],
    }
    assert not requests


def test_exchange_stores_tokens_and_expiry(setup_manager):
    setup, store, requests = setup_manager
    manager = setup(
        httpx.Response(
            200,
            json={
                "access_token": "access value",
                "refresh_token": "refresh value",
                "expires_in": 86400,
            },
        )
    )
    expiry = NOW + timedelta(days=1)
    assert manager.complete_renewal(CODE) == expiry
    (request,) = requests
    assert request.method == "POST"
    assert str(request.url) == "https://www.linkedin.com/oauth/v2/accessToken"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert parse_qs(request.content.decode()) == {
        "grant_type": ["authorization_code"],
        "code": [CODE],
        "redirect_uri": [REDIRECT],
        "client_id": ["test-id"],
        "client_secret": [CLIENT_SECRET],
    }
    store.save.assert_called_once_with(
        "linkedin", "access value", expiry, "refresh value"
    )
    store.get.return_value = StoredToken("linkedin", "access value", expiry)
    assert manager.days_left(NOW) == 1


def test_refresh_requires_token_and_preserves_it_if_omitted(setup_manager):
    setup, store, requests = setup_manager
    manager = setup(
        httpx.Response(200, json={"access_token": "new access", "expires_in": 60})
    )
    assert manager.days_left(NOW) is None
    assert not manager.try_refresh()
    store.get.return_value = StoredToken("linkedin", "old access", NOW)
    assert not manager.try_refresh()
    assert not requests

    store.get.return_value = StoredToken("linkedin", "old access", NOW, "old refresh")
    assert manager.try_refresh()
    (request,) = requests
    assert parse_qs(request.content.decode()) == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["old refresh"],
        "client_id": ["test-id"],
        "client_secret": [CLIENT_SECRET],
    }
    store.save.assert_called_once_with(
        "linkedin",
        "new access",
        NOW + timedelta(seconds=60),
        "old refresh",
    )


@pytest.mark.parametrize("status", [400, 500, 200])
def test_exchange_error_is_sanitized(setup_manager, status):
    setup, store, _ = setup_manager
    manager = setup(httpx.Response(status, text=f"{CODE} {CLIENT_SECRET} {REDIRECT}"))
    with pytest.raises(RenewalError) as error:
        manager.complete_renewal(CODE)
    assert str(error.value) == "LinkedIn token exchange failed"
    assert all(
        value not in repr(error.value) for value in (CODE, CLIENT_SECRET, REDIRECT)
    )
    assert error.value.__context__ is None
    store.save.assert_not_called()
