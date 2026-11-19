import json
from unittest.mock import Mock

import httpx
import pytest
from psycopg import Error

from postpilot.config import ShortioSettings
from postpilot.links.shortio import ShortioClient, tracked_link

_KEY = "mock-shortio-secret"
_URL = "https://github.com/owner/repo"
_SHORT_URL = "https://example.short.gy/a"


def test_create_track_and_get_clicks():
    def respond(request):
        assert request.headers["Authorization"] == _KEY
        if request.method == "POST":
            assert str(request.url) == "https://api.short.io/links"
            payload = json.loads(request.content)
            assert payload["originalURL"] == _URL
            assert payload["domain"] == "example.short.gy"
            assert payload["allowDuplicates"] is True
            assert payload["title"].startswith("repo — de — ")
            return httpx.Response(
                200, json={"idString": "link-id", "shortURL": _SHORT_URL}
            )
        assert request.url.host == "api-v2.short.io"
        assert request.url.path == "/statistics/link/link-id"
        assert dict(request.url.params) == {"period": "total", "tzOffset": "0"}
        return httpx.Response(200, json={"totalClicks": 0})

    client = ShortioClient(
        ShortioSettings(_KEY, "example.short.gy"),
        transport=httpx.MockTransport(respond),
    )
    store = Mock()
    try:
        assert client.create_link(_URL, "repo", "de") == _SHORT_URL
        assert tracked_link(client, store, _URL, "repo", "de") == _SHORT_URL
        link = store.save_link.call_args.args[0]
        assert (link.repo, link.lang, link.destination) == ("repo", "de", _URL)
        assert client.total_clicks(link.link_id) == 0
        store.save_link.side_effect = Error(_KEY)
        assert tracked_link(client, store, _URL, "repo", "de") == _URL
    finally:
        client.close()


@pytest.mark.parametrize("status", [200, 403, 500, None])
def test_fail_open_without_logging_secrets(status, caplog):
    def respond(request):
        if status is None:
            raise httpx.ConnectError(_KEY, request=request)
        return httpx.Response(status, json={"error": _KEY})

    client = ShortioClient(
        ShortioSettings(_KEY, "example.short.gy"),
        transport=httpx.MockTransport(respond),
    )
    store = Mock()
    try:
        assert client.create_link(_URL, "repo", "de") is None
        assert client.total_clicks("link-id") is None
        assert tracked_link(client, store, _URL, "repo", "de") == _URL
        assert tracked_link(None, store, _URL, "repo", "de") == _URL
        store.save_link.assert_not_called()
        assert _KEY not in caplog.text
    finally:
        client.close()
