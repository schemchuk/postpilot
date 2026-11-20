import logging
from datetime import timedelta

import httpx

from postpilot.channels.base import TransientChannelError
from postpilot.config import FacebookSettings, InstagramSettings

PAGE_TOKEN_STORAGE_LIFETIME = timedelta(days=3650)


def _exclude_debug_token(record: logging.LogRecord) -> bool:
    return "/debug_token?" not in record.getMessage()


logging.getLogger("httpx").addFilter(_exclude_debug_token)


def token_is_valid(
    settings: FacebookSettings | InstagramSettings, client: httpx.Client, token: str
) -> bool:
    return debug_token(settings.graph_url, client, token)["is_valid"] is True


def debug_token(graph_url: str, client: httpx.Client, token: str) -> dict[str, object]:
    try:
        response = client.get(
            f"{graph_url}/debug_token",
            params={"input_token": token},
            headers={"Authorization": f"OAuth {token}"},
            timeout=30,
            follow_redirects=False,
        )
        result = response.json()
    except (httpx.RequestError, ValueError):
        raise TransientChannelError("Meta token validation unavailable") from None
    if isinstance(result, dict):
        error = result.get("error")
        if isinstance(error, dict) and error.get("code") == 190:
            return {"is_valid": False}
        data = result.get("data")
        if (
            response.is_success
            and isinstance(data, dict)
            and isinstance(data.get("is_valid"), bool)
        ):
            return data
    raise TransientChannelError("Meta token validation unavailable")
