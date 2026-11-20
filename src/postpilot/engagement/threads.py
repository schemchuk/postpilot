from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import httpx

_API = "https://graph.threads.com/v1.0"
_FIELDS = "id,text,username,timestamp,permalink,is_reply_owned_by_me"
_MAX_PAGES = 5


class ThreadsEngagementError(RuntimeError):
    pass


@dataclass(frozen=True)
class Reply:
    id: str
    text: str
    username: str
    timestamp: datetime
    permalink: str


def _required_string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ThreadsEngagementError("Invalid Threads response field")
    return value


class ThreadsEngagementClient:
    def __init__(self, client: httpx.Client, token: str) -> None:
        self._client = client
        self._token = token
        self._username: str | None = None

    def _request(self, path: str, params: dict[str, str | int]) -> dict[str, object]:
        try:
            response = self._client.get(
                f"{_API}/{path}",
                headers={"Authorization": f"Bearer {self._token}"},
                params=params,
                follow_redirects=False,
            )
            if response.is_success:
                result = response.json()
                if isinstance(result, dict):
                    return result
        except (httpx.HTTPError, httpx.StreamError, httpx.InvalidURL, ValueError):
            pass
        raise ThreadsEngagementError("Threads engagement request failed")

    def conversation(self, media_id: str) -> list[Reply]:
        _required_string(media_id)
        if self._username is None:
            profile = self._request("me", {"fields": "id,username"})
            _required_string(profile.get("id"))
            self._username = _required_string(profile.get("username"))

        replies: list[Reply] = []
        seen: set[str] = set()
        params: dict[str, str | int] = {"fields": _FIELDS, "limit": 50}
        path = f"{quote(media_id, safe='')}/conversation"
        for _ in range(_MAX_PAGES):
            page = self._request(path, params)
            rows = page.get("data")
            if not isinstance(rows, list):
                raise ThreadsEngagementError("Invalid Threads conversation")
            if not rows:
                break
            for row in rows:
                reply = self._parse_reply(row, self._username)
                if reply is not None:
                    replies.append(reply)

            paging = page.get("paging", {})
            cursors = paging.get("cursors", {}) if isinstance(paging, dict) else None
            if not isinstance(cursors, dict):
                raise ThreadsEngagementError("Invalid Threads pagination")
            after = cursors.get("after")
            if after is None or after == "":
                break
            cursor = _required_string(after)
            if cursor in seen:
                break
            seen.add(cursor)
            params["after"] = cursor
        return replies

    @staticmethod
    def _parse_reply(row: object, own_username: str) -> Reply | None:
        if not isinstance(row, dict):
            raise ThreadsEngagementError("Invalid Threads reply")
        owned = row.get("is_reply_owned_by_me", False)
        if not isinstance(owned, bool):
            raise ThreadsEngagementError("Invalid Threads reply ownership")
        if owned:
            return None
        username = _required_string(row.get("username"))
        if username == own_username:
            return None
        text = row.get("text")
        if text is None or (isinstance(text, str) and not text.strip()):
            return None
        try:
            timestamp = datetime.fromisoformat(_required_string(row.get("timestamp")))
        except ValueError:
            raise ThreadsEngagementError("Invalid Threads reply timestamp") from None
        if timestamp.utcoffset() is None:
            raise ThreadsEngagementError("Invalid Threads reply timestamp")
        return Reply(
            _required_string(row.get("id")),
            _required_string(text),
            username,
            timestamp,
            _required_string(row.get("permalink")),
        )
