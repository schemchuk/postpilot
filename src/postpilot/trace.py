"""One-line run traces for the live analysis of a job (ADR 018 amendment, T18c).

Actions logs of a public repository are public. A trace line therefore carries only
ids, states, counts, lengths, booleans, short labels and HTTP status codes: never post
text, comment text, tokens or URLs with query strings. String values are shortened,
stripped of whitespace and query strings and passed through the secret redaction used
for failure alerts.
"""

import re
import sys
from collections.abc import Iterable
from enum import Enum
from uuid import UUID

import httpx

from postpilot.jobs.alerts import redact

_MAX_VALUE = 80
_secrets: frozenset[str] = frozenset()


def set_secrets(values: Iterable[str]) -> None:
    global _secrets
    _secrets = frozenset(value for value in values if value)


def _value(value: object) -> str:
    if value is None or isinstance(value, bool | int | float | UUID):
        return str(value)
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, list | tuple | set | frozenset):
        items = sorted(value, key=str) if isinstance(value, set | frozenset) else value
        return ",".join(_value(item) for item in items) or "-"
    text = redact(str(value).split("?", 1)[0], _secrets)
    text = re.sub(r"\s+", "_", text)
    return text if len(text) <= _MAX_VALUE else text[: _MAX_VALUE - 1] + "…"


def trace(scope: str, event: str, **fields: object) -> None:
    line = " ".join(
        [f"trace {scope} {event}", *(f"{key}={_value(v)}" for key, v in fields.items())]
    )
    print(line, file=sys.stdout, flush=True)


def _safe_path(request: httpx.Request) -> str:
    # Telegram puts the bot token in the path; drop it before anything else.
    return re.sub(r"/bot[^/]+", "/bot***", request.url.path)


def error_fields(error: BaseException) -> dict[str, object]:
    """Type, HTTP status and request path of an error and its causes, no message."""
    fields: dict[str, object] = {"error": type(error).__name__}
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.HTTPStatusError):
            fields["http_status"] = current.response.status_code
            fields["http"] = f"{current.request.method}_{_safe_path(current.request)}"
            break
        if isinstance(current, httpx.RequestError):
            fields["http_error"] = type(current).__name__
            try:
                request = current.request
            except RuntimeError:
                break
            fields["http"] = f"{request.method}_{_safe_path(request)}"
            break
        current = current.__cause__ or current.__context__
    return fields
