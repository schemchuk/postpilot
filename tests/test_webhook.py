import json
from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, call
from wsgiref.util import setup_testing_defaults
from wsgiref.validate import validator

import httpx
import psycopg
import pytest

from postpilot import webhook

USER = 12345678901
CALLBACK = {
    "update_id": 42,
    "callback_query": {"id": "button", "from": {"id": USER}},
}
MESSAGE = {
    "update_id": 43,
    "message": {"text": "private text", "from": {"id": USER}},
}
CONFIG = {
    "TELEGRAM_BOT_TOKEN": "fake-bot",
    "TELEGRAM_WEBHOOK_SECRET": "fake-secret",
    "DATABASE_URL": "fake-db",
    "TELEGRAM_CHAT_ID": str(USER),
    "GITHUB_DISPATCH_TOKEN": "fake-pat",
}


@pytest.fixture
def receiver(monkeypatch):
    config = CONFIG.copy()
    rows = {}
    closed = []

    @contextmanager
    def connection(url, *, connect_timeout):
        assert url == "fake-db"
        assert connect_timeout == 10
        try:
            yield "fresh connection"
        finally:
            closed.append(True)

    def insert(update):
        update_id = update["update_id"]
        if update_id in rows:
            return False
        rows[update_id] = update
        return True

    inbox = Mock(insert=Mock(side_effect=insert))
    monkeypatch.setattr(webhook, "UpdateInbox", Mock(return_value=inbox))
    connect = Mock(side_effect=connection)
    http = Mock()
    app = webhook.create_app(config, connect=connect, http=http)

    def request(update=CALLBACK, *, body=None, method="POST", secret="fake-secret"):
        if body is None:
            body = json.dumps(update).encode()
        environ = {}
        setup_testing_defaults(environ)
        environ.update(
            REQUEST_METHOD=method,
            CONTENT_LENGTH=str(len(body)),
            QUERY_STRING="",
        )
        environ["wsgi.input"] = BytesIO(body)
        if secret is not None:
            environ["HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN"] = secret
        start_response = Mock()
        result = validator(app)(environ, start_response)
        try:
            data = json.loads(b"".join(result))
        finally:
            result.close()
        return int(start_response.call_args.args[0].split()[0]), data

    return SimpleNamespace(
        request=request,
        config=config,
        rows=rows,
        closed=closed,
        connect=connect,
        inbox=inbox,
        http=http,
    )


@pytest.mark.parametrize("secret", [None, "wrong"])
def test_webhook_rejects_missing_or_wrong_secret(receiver, secret):
    assert receiver.request(secret=secret)[0] == 401
    receiver.connect.assert_not_called()
    receiver.http.post.assert_not_called()


@pytest.mark.parametrize(
    "update",
    [
        {"update_id": 42, "message": {"from": {"id": USER + 1}}},
        {"update_id": 42, "callback_query": {"from": {"id": USER + 1}}},
        {"update_id": 42, "edited_message": {"from": {"id": USER}}},
    ],
)
def test_webhook_ignores_foreign_users_and_other_update_types(receiver, update):
    assert receiver.request(update) == (200, {"ok": True})
    receiver.connect.assert_not_called()
    receiver.http.post.assert_not_called()


def test_webhook_stores_and_answers_callback_once_and_dispatches_each_delivery(
    receiver,
):
    assert receiver.request() == (200, {"ok": True})
    assert receiver.request() == (200, {"ok": True})
    assert receiver.rows == {42: CALLBACK}
    assert receiver.closed == [True, True]
    dispatch = call(
        "https://api.github.com/repos/schemchuk/postpilot/actions/workflows/poll-telegram.yml/dispatches",
        timeout=5,
        json={"ref": "main"},
        headers={
            "Authorization": "Bearer fake-pat",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    assert receiver.http.post.call_args_list == [
        call(
            "https://api.telegram.org/botfake-bot/answerCallbackQuery",
            timeout=5,
            json={"callback_query_id": "button", "text": "Received, working on it"},
            headers=None,
        ),
        dispatch,
        dispatch,
    ]


def test_webhook_stores_message_and_dispatches_to_configured_repo(receiver):
    receiver.config["GITHUB_REPOSITORY"] = "owner/other"
    assert receiver.request(MESSAGE) == (200, {"ok": True})
    assert receiver.rows == {43: MESSAGE}
    assert receiver.http.post.call_count == 1
    assert "/repos/owner/other/actions/" in receiver.http.post.call_args.args[0]


def test_webhook_returns_500_when_insert_fails(receiver, caplog):
    receiver.inbox.insert.side_effect = psycopg.OperationalError("private text")
    assert receiver.request()[0] == 500
    assert receiver.closed == [True]
    receiver.http.post.assert_not_called()
    assert caplog.messages == ["Telegram inbox insert failed (OperationalError)"]


@pytest.mark.parametrize("failing_request", [0, 1], ids=["callback", "dispatch"])
def test_outbound_failure_is_best_effort(receiver, caplog, failing_request):
    results = [Mock(), Mock()]
    results[failing_request].raise_for_status.side_effect = httpx.HTTPStatusError(
        "private text", request=Mock(), response=Mock()
    )
    receiver.http.post.side_effect = results
    assert receiver.request() == (200, {"ok": True})
    assert receiver.rows == {42: CALLBACK}
    assert receiver.http.post.call_count == 2
    action = ["Telegram callback answer", "GitHub workflow dispatch"][failing_request]
    assert caplog.messages == [f"{action} failed (HTTPStatusError)"]


def test_webhook_rejects_non_post_before_checking_config(receiver):
    receiver.config.clear()
    assert receiver.request(method="GET")[0] == 405
    receiver.connect.assert_not_called()


@pytest.mark.parametrize("name", CONFIG)
def test_webhook_missing_config_logs_only_name(receiver, caplog, name):
    del receiver.config[name]
    assert receiver.request()[0] == 500
    assert caplog.messages == [f"Webhook config missing: {name}"]
    receiver.connect.assert_not_called()


@pytest.mark.parametrize("body", [b"{", b"[]", b"{}", b'{"update_id": true}'])
def test_webhook_rejects_invalid_body(receiver, body):
    assert receiver.request(body=body)[0] == 400
    receiver.connect.assert_not_called()


def test_webhook_hides_bot_token_from_httpx_request_logging(receiver, caplog):
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200))
        ) as client,
        caplog.at_level("INFO", logger="httpx"),
    ):
        receiver.http.post.side_effect = client.post
        assert receiver.request()[0] == 200
    assert "api.github.com" in caplog.text
    assert "fake-bot" not in caplog.text
