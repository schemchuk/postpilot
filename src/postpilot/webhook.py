"""Receive Telegram updates and wake the inbox consumer."""

import hmac
import json
import logging
import os
from collections.abc import Mapping
from typing import Protocol
from wsgiref.types import StartResponse, WSGIApplication, WSGIEnvironment

import httpx
import psycopg
from psycopg.rows import TupleRow

from postpilot.store.update_inbox import UpdateInbox

logger = logging.getLogger(__name__)
_REQUIRED = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "DATABASE_URL",
    "TELEGRAM_CHAT_ID",
    "GITHUB_DISPATCH_TOKEN",
)


class Connect(Protocol):
    def __call__(
        self, url: str, /, *, connect_timeout: int
    ) -> psycopg.Connection[TupleRow]: ...


class Http(Protocol):
    def post(
        self,
        url: str,
        /,
        *,
        timeout: float,
        json: dict[str, str],
        headers: dict[str, str] | None = None,
    ) -> httpx.Response: ...


def _hide_telegram_url(record: logging.LogRecord) -> bool:
    # HTTPX logs request URLs at INFO. Telegram includes the bot token in its URL.
    return "https://api.telegram.org/bot" not in record.getMessage()


def _respond(start_response: StartResponse, status: str) -> list[bytes]:
    body = b'{"ok": true}' if status == "200 OK" else b'{"ok": false}'
    start_response(
        status,
        [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
    )
    return [body]


def _post(
    http: Http,
    action: str,
    url: str,
    *,
    json: dict[str, str],
    headers: dict[str, str] | None = None,
) -> None:
    try:
        http.post(url, timeout=5, json=json, headers=headers).raise_for_status()
    except Exception as error:  # noqa: BLE001 - delivery is best effort
        logger.warning("%s failed (%s)", action, type(error).__name__)


def create_app(
    config: Mapping[str, str], *, connect: Connect, http: Http
) -> WSGIApplication:
    def receive(environ: WSGIEnvironment, start_response: StartResponse) -> list[bytes]:
        if environ.get("REQUEST_METHOD") != "POST":
            return _respond(start_response, "405 Method Not Allowed")

        for name in _REQUIRED:
            if not config.get(name):
                logger.error("Webhook config missing: %s", name)
                return _respond(start_response, "500 Internal Server Error")
        try:
            allowed_user = int(config["TELEGRAM_CHAT_ID"])
        except ValueError:
            logger.error("TELEGRAM_CHAT_ID")
            return _respond(start_response, "500 Internal Server Error")

        secret = environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", "")
        if not hmac.compare_digest(
            secret.encode(), config["TELEGRAM_WEBHOOK_SECRET"].encode()
        ):
            return _respond(start_response, "401 Unauthorized")

        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
            update = json.loads(environ["wsgi.input"].read(max(length, 0)))
        except (ValueError, RecursionError):
            return _respond(start_response, "400 Bad Request")
        if not isinstance(update, dict) or type(update.get("update_id")) is not int:
            return _respond(start_response, "400 Bad Request")

        callback = update.get("callback_query")
        event = callback if callback is not None else update.get("message")
        sender = event.get("from") if isinstance(event, dict) else None
        sender_id = sender.get("id") if isinstance(sender, dict) else None
        if type(sender_id) is not int or sender_id != allowed_user:
            return _respond(start_response, "200 OK")

        try:
            with connect(config["DATABASE_URL"], connect_timeout=10) as conn:
                inserted = UpdateInbox(conn).insert(update)
        except Exception as error:  # noqa: BLE001 - let Telegram retry storage failures
            logger.warning("Telegram inbox insert failed (%s)", type(error).__name__)
            return _respond(start_response, "500 Internal Server Error")

        callback_id = callback.get("id") if isinstance(callback, dict) else None
        if inserted and isinstance(callback_id, str):
            _post(
                http,
                "Telegram callback answer",
                f"https://api.telegram.org/bot{config['TELEGRAM_BOT_TOKEN']}/answerCallbackQuery",
                json={
                    "callback_query_id": callback_id,
                    "text": "Received, working on it",
                },
            )

        repository = config.get("GITHUB_REPOSITORY") or "schemchuk/postpilot"
        _post(
            http,
            "GitHub workflow dispatch",
            f"https://api.github.com/repos/{repository}/actions/workflows/poll-telegram.yml/dispatches",
            headers={
                "Authorization": f"Bearer {config['GITHUB_DISPATCH_TOKEN']}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"ref": "main"},
        )
        return _respond(start_response, "200 OK")

    return receive


logging.getLogger("httpx").addFilter(_hide_telegram_url)
app = create_app(os.environ, connect=psycopg.connect, http=httpx)
