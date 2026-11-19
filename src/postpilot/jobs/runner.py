import sys
from collections.abc import Callable

from postpilot.config import Settings
from postpilot.jobs.alerts import github_run_url, report_failure
from postpilot.notify.telegram import Notifier, TelegramApi


def run_job(name: str, func: Callable[[], object], settings: Settings) -> int:
    notifier = Notifier(
        TelegramApi(settings.telegram_bot_token), settings.telegram_chat_id
    )
    try:
        func()
    except Exception as error:  # noqa: BLE001 - report job failures
        print(f"{name} failed ({type(error).__name__})", file=sys.stderr)
        report_failure(
            name, error, notifier.send_alert, settings.secret_values(), github_run_url()
        )
        return 1
    return 0
