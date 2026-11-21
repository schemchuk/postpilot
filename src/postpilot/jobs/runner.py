import sys
import time
from collections.abc import Callable

from postpilot.config import Settings
from postpilot.jobs.alerts import github_run_url, report_failure
from postpilot.notify.telegram import Notifier, TelegramApi
from postpilot.trace import error_fields, set_secrets, trace


def run_job(name: str, func: Callable[[], object], settings: Settings) -> int:
    notifier = Notifier(
        TelegramApi(settings.telegram_bot_token), settings.telegram_chat_id
    )
    set_secrets(settings.secret_values())
    started = time.monotonic()
    trace(name, "start")
    try:
        func()
    except Exception as error:  # noqa: BLE001 - report job failures
        trace(
            name,
            "failed",
            seconds=round(time.monotonic() - started, 1),
            **error_fields(error),
        )
        print(f"{name} failed ({type(error).__name__})", file=sys.stderr)
        report_failure(
            name, error, notifier.send_alert, settings.secret_values(), github_run_url()
        )
        return 1
    trace(name, "done", seconds=round(time.monotonic() - started, 1))
    return 0
