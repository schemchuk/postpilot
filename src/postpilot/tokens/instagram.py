from datetime import UTC, datetime, timedelta

import httpx

from postpilot.config import InstagramSettings
from postpilot.tokens.facebook import debug_token


def token_expiry(
    settings: InstagramSettings, client: httpx.Client, token: str, now: datetime
) -> datetime:
    data = debug_token(settings.graph_url, client, token)
    if data["is_valid"] is not True:
        raise ValueError("Instagram token is invalid")
    expiries = [
        datetime.fromtimestamp(value, UTC)
        for key in ("expires_at", "data_access_expires_at")
        if isinstance(value := data.get(key), int) and value > 0
    ]
    return min(expiries, default=now + timedelta(days=60))
