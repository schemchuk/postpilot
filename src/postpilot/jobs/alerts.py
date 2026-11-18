import logging
import os
import re
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)
_TOKEN_SHAPES = (
    r"\b\d{6,}:[A-Za-z0-9_-]{30,}"
    r"|\bpostgres(?:ql)?://\S+"
    r"|\bBearer\s+\S+"
    r"|[A-Za-z0-9_+/=-]{32,}"
)


def redact(text: str, secrets: Iterable[str]) -> str:
    spans = [match.span() for match in re.finditer(_TOKEN_SHAPES, text, re.IGNORECASE)]
    for secret in set(secrets):
        if len(secret) >= 6:
            spans.extend(
                match.span(1)
                for match in re.finditer(f"(?=({re.escape(secret)}))", text)
            )
    parts: list[str] = []
    end = 0
    for start, stop in sorted(spans):
        if start >= end:
            parts.extend((text[end:start], "***"))
        end = max(end, stop)
    return "".join([*parts, text[end:]])


def github_run_url() -> str | None:
    server = os.getenv("GITHUB_SERVER_URL")
    repository = os.getenv("GITHUB_REPOSITORY")
    run_id = os.getenv("GITHUB_RUN_ID")
    if server and repository and run_id:
        return f"{server.rstrip('/')}/{repository}/actions/runs/{run_id}"
    return None


def report_failure(
    job: str,
    error: Exception,
    notifier_send: Callable[[str], None],
    secrets: Iterable[str],
    run_url: str | None,
) -> None:
    try:
        message = redact(f"{job} failed ({type(error).__name__}): {error}", secrets)
        link = f"\n{run_url}" if run_url else ""
        limit = 500 - len(link)
        notifier_send(
            (message if len(message) <= limit else message[: limit - 1] + "…") + link
        )
    except Exception as send_error:  # noqa: BLE001 - alert delivery is best effort
        logger.warning("Failure alert failed (%s)", type(send_error).__name__)
