import logging
from datetime import timedelta

import httpx

from postpilot.channels.base import TransientChannelError
from postpilot.config import FacebookSettings

PAGE_TOKEN_STORAGE_LIFETIME = timedelta(days=3650)


def _exclude_debug_token(record: logging.LogRecord) -> bool:
    return "/debug_token?" not in record.getMessage()


logging.getLogger("httpx").addFilter(_exclude_debug_token)


def token_is_valid(
    settings: FacebookSettings, client: httpx.Client, token: str
) -> bool:
    try:
        response = client.get(
            f"{settings.graph_url}/debug_token",
            params={"input_token": token},
            headers={"Authorization": f"OAuth {token}"},
            timeout=30,
            follow_redirects=False,
        )
        result = response.json()
    except (httpx.RequestError, ValueError):
        raise TransientChannelError("Facebook token validation unavailable") from None
    if isinstance(result, dict):
        error = result.get("error")
        if isinstance(error, dict) and error.get("code") == 190:
            return False
        data = result.get("data")
        if (
            response.is_success
            and isinstance(data, dict)
            and isinstance(data.get("is_valid"), bool)
        ):
            return bool(data["is_valid"])
    raise TransientChannelError("Facebook token validation unavailable")
