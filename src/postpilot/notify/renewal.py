import re
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime

from postpilot.notify.telegram import TelegramApi, TelegramError
from postpilot.tokens.base import TokenManager
from postpilot.tokens.oauth_state import StateStore, verify_state

_COMMAND = re.compile(r"/renew(?:\s|$)")
_ARGUMENTS = re.compile(r"/renew ([^\s]+) ([^\s]+)")


class RenewalHandler:
    def __init__(
        self,
        managers: dict[str, TokenManager],
        secret_key: bytes,
        api: TelegramApi,
        on_renewed: Callable[[str], None] | None = None,
        *,
        state_store: StateStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._managers = managers
        self._secret_key = secret_key
        self._api = api
        self._on_renewed = on_renewed
        self._state_store = state_store
        self._clock = clock

    def handle_message(self, chat_id: int, message_id: int, text: str) -> bool:
        if _COMMAND.match(text) is None:
            return False

        with suppress(TelegramError):
            self._api.delete_message(chat_id, message_id)
        arguments = _ARGUMENTS.fullmatch(text)
        if arguments is None:
            self._api.send_message(
                chat_id, "Use /renew <code> <state> from the callback page."
            )
            return True

        code, state = arguments.groups()
        try:
            provider = verify_state(
                self._secret_key, state, self._clock(), store=self._state_store
            )
            expiry = self._managers[provider].complete_renewal(code)
        except Exception:  # noqa: BLE001 - never expose renewal failure details
            self._api.send_message(
                chat_id, "Token renewal failed. Use a fresh renewal link."
            )
            return True

        label = "LinkedIn" if provider == "linkedin" else provider
        until = f" until {expiry.date().isoformat()}" if expiry is not None else ""
        try:
            self._api.send_message(chat_id, f"{label} token renewed{until}")
        finally:
            if self._on_renewed is not None:
                self._on_renewed(provider)
        return True
