import httpx

from postpilot.channels.base import (
    ChannelError,
    ChannelHeld,
    TransientChannelError,
    UncertainPublication,
)


def request(
    client: httpx.Client,
    channel: str,
    method: str,
    url: str,
    token: str,
    *,
    data: dict[str, str] | None = None,
    video: bytes | None = None,
    offset: int = 0,
    finish: bool = False,
) -> dict[str, object]:
    retry = UncertainPublication if finish else TransientChannelError
    headers = {"Authorization": f"OAuth {token}"}
    if video is not None:
        headers.update(
            {
                "offset": str(offset),
                "file_size": str(len(video)),
                "Content-Type": "application/octet-stream",
            }
        )
    try:
        response = client.request(
            method,
            url,
            headers=headers,
            data=data,
            content=video[offset:] if video is not None else None,
            timeout=60,
            follow_redirects=False,
        )
        result = response.json()
    except httpx.RequestError:
        raise retry(f"{channel} request failed") from None
    except ValueError:
        result = None
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict):
        code = error.get("code")
        if code == 190:
            raise ChannelHeld(f"{channel} token is invalid")
        if error.get("is_transient") is True or code in (1, 2, 4, 17, 32, 341, 613):
            raise retry(f"{channel} temporarily unavailable")
    if response.status_code >= 500:
        raise retry(f"{channel} service unavailable")
    if response.status_code == 429:
        raise retry(f"{channel} rate limit reached")
    if not response.is_success:
        raise ChannelError(f"{channel} request rejected (HTTP {response.status_code})")
    if not isinstance(result, dict):
        raise retry(f"Invalid {channel} response")
    return result
