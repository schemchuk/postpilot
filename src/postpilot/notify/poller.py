import logging

from postpilot.notify.approval import ApprovalHandler
from postpilot.notify.telegram import TelegramApi
from postpilot.store.bot_state import BotState

logger = logging.getLogger(__name__)


def poll_once(api: TelegramApi, handler: ApprovalHandler, state: BotState) -> None:
    updates = api.get_updates(state.get_offset())
    for update in updates:
        try:
            handler.handle_update(update)
        except Exception as error:  # noqa: BLE001 - isolate each update
            logger.warning("Telegram update handler failed (%s)", type(error).__name__)
    if updates:
        state.set_offset(int(updates[-1]["update_id"]) + 1)
