import logging

from postpilot.notify.approval import ApprovalHandler
from postpilot.store.update_inbox import PENDING_BATCH_SIZE, UpdateInbox

logger = logging.getLogger(__name__)


def drain_inbox(
    inbox: UpdateInbox,
    handler: ApprovalHandler,
    *,
    raise_errors: bool = False,
) -> None:
    failure = None
    batch = inbox.pending()
    while batch:
        for update_id, payload in batch:
            try:
                handler.handle_update(payload)
            except Exception as error:  # noqa: BLE001 - isolate each update
                logger.warning(
                    "Telegram update handler failed (%s)", type(error).__name__
                )
                if failure is None:
                    failure = error
                if inbox.record_failure(update_id):
                    logger.warning(
                        "Telegram update %s dropped after 3 failures", update_id
                    )
            else:
                inbox.mark_processed(update_id)
        if len(batch) < PENDING_BATCH_SIZE:
            break
        batch = inbox.pending(after_update_id=batch[-1][0])
    inbox.purge(7)
    if raise_errors and failure is not None:
        raise failure
