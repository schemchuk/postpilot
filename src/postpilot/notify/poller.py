import logging

from postpilot.notify.approval import ApprovalHandler
from postpilot.notify.telegram import TelegramApi, TelegramError
from postpilot.store.bot_state import BotState

logger = logging.getLogger(__name__)


def poll_once(
    api: TelegramApi,
    handler: ApprovalHandler,
    state: BotState,
    *,
    raise_errors: bool = False,
) -> None:
    failure = None
    updates = api.get_updates(state.get_offset())
    for update in updates:
        try:
            handler.handle_update(update)
        except Exception as error:  # noqa: BLE001 - isolate each update
            logger.warning("Telegram update handler failed (%s)", type(error).__name__)
            failure = error
    if updates:
        new_offset = int(updates[-1]["update_id"]) + 1
        state.set_offset(new_offset)
        try:
            # Any updates returned here stay pending for the next poll.
            api.get_updates(offset=new_offset, timeout=0, limit=1)
        except TelegramError as error:
            logger.warning(
                "Telegram update confirmation failed (%s)", type(error).__name__
            )
    if raise_errors and failure is not None:
        raise failure
