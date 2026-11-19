"""Command-line entry point."""

import argparse
from collections.abc import Callable
from functools import partial
from pathlib import Path

from psycopg import Connection
from psycopg.rows import TupleRow

from postpilot import __version__
from postpilot.config import ConfigError, Settings
from postpilot.jobs import tasks
from postpilot.jobs.runner import run_job
from postpilot.store import migrate
from postpilot.store.db import connect

_DATABASE_COMMANDS = {
    "poll-telegram": tasks.poll_telegram,
    "publish-due": tasks.publish_due,
    "check-tokens": tasks.check_tokens,
}
_COMMANDS = (
    "draft-weekly",
    "poll-telegram",
    "publish-due",
    "snapshot-traffic",
    "check-tokens",
)


def _with_database(
    job: Callable[[Settings, Connection[TupleRow]], None], settings: Settings
) -> None:
    with connect(settings.database_url) as conn:
        migrate.up(conn)
        job(settings, conn)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="postpilot", description=f"postpilot {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command")
    for command in _COMMANDS:
        subparsers.add_parser(command)

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    try:
        settings = Settings.from_env(Path(".env"))
    except ConfigError as error:
        parser.error(str(error))

    database_job = _DATABASE_COMMANDS.get(args.command)
    operation = (
        partial(_with_database, database_job, settings)
        if database_job is not None
        else tasks.not_implemented
    )
    return run_job(args.command, operation, settings)


if __name__ == "__main__":
    raise SystemExit(main())
