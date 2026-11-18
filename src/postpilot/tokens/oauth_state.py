import hmac
import re
import secrets
from datetime import datetime, timedelta
from typing import Protocol

from postpilot.tokens.base import utc_datetime

_FORMAT = re.compile(
    r"([a-z][a-z0-9_-]*)\.([0-9a-f]{32})\.([0-9]{1,12})\.([0-9a-f]{64})"
)
_MAX_AGE = timedelta(minutes=30)


class InvalidState(ValueError):
    pass


class StateStore(Protocol):
    def save_oauth_nonce(self, nonce: str) -> None: ...
    def consume_oauth_nonce(self, nonce: str) -> bool: ...


def _signature(secret_key: bytes, payload: str) -> str:
    key = hmac.digest(secret_key, b"postpilot/oauth-state/v1", "sha256")
    return hmac.digest(key, payload.encode("ascii"), "sha256").hex()


def make_state(
    secret_key: bytes, provider: str, now: datetime, *, store: StateStore | None = None
) -> str:
    if re.fullmatch(r"[a-z][a-z0-9_-]*", provider) is None:
        raise InvalidState("Invalid OAuth state")
    nonce = secrets.token_hex(16)
    payload = f"{provider}.{nonce}.{int(utc_datetime(now).timestamp())}"
    state = f"{payload}.{_signature(secret_key, payload)}"
    if store is not None:
        store.save_oauth_nonce(nonce)
    return state


def verify_state(
    secret_key: bytes,
    state: str,
    now: datetime,
    max_age: timedelta = _MAX_AGE,
    *,
    store: StateStore | None = None,
) -> str:
    match = _FORMAT.fullmatch(state)
    if match is None:
        raise InvalidState("Invalid OAuth state")
    provider, nonce, timestamp, signature = match.groups()
    payload = state.rsplit(".", 1)[0]
    age = utc_datetime(now).timestamp() - int(timestamp)
    if not hmac.compare_digest(signature, _signature(secret_key, payload)) or not (
        0 <= age <= max_age.total_seconds()
    ):
        raise InvalidState("Invalid OAuth state")
    if store is not None and not store.consume_oauth_nonce(nonce):
        raise InvalidState("Invalid OAuth state")
    return provider
