"""Command-line entry point."""

import argparse
import json
import logging
from collections.abc import Callable
from datetime import date
from functools import partial
from pathlib import Path

from psycopg import Connection, Error
from psycopg.rows import TupleRow

from postpilot import __version__
from postpilot.config import ConfigError, Settings
from postpilot.core.limits import LINKEDIN_TEXT_LIMIT
from postpilot.jobs import tasks
from postpilot.jobs.runner import run_job
from postpilot.sources.series import ActiveSeriesExists, SeriesStore
from postpilot.store import migrate
from postpilot.store.db import connect

_DATABASE_COMMANDS = {
    "draft-weekly": tasks.draft_weekly,
    "poll-engagement": tasks.poll_engagement,
    "poll-telegram": tasks.poll_telegram,
    "publish-due": tasks.publish_due,
    "check-tokens": tasks.check_tokens,
    "snapshot-traffic": tasks.snapshot_traffic,
    "facebook-import-token": tasks.facebook_import_token,
    "instagram-import-token": tasks.instagram_import_token,
    "threads-import-token": tasks.threads_import_token,
}
_COMMANDS = (
    "series-import",
    "draft-weekly",
    "poll-engagement",
    "poll-telegram",
    "publish-due",
    "render-video",
    "publish-reels",
    "facebook-import-token",
    "instagram-import-token",
    "snapshot-traffic",
    "check-tokens",
    "threads-import-token",
)


def _with_database(
    job: Callable[[Settings, Connection[TupleRow]], None], settings: Settings
) -> None:
    with connect(settings.database_url) as conn:
        migrate.up(conn)
        job(settings, conn)


def _import_array(data: dict[str, object], field: str) -> list[object]:
    value = data.get(field)
    if not isinstance(value, list):
        raise TypeError(f"{field} must be an array")
    return value


def _import_date(value: object, field: str) -> date:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f"{field} must be an ISO date")


def _import_text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > LINKEDIN_TEXT_LIMIT
        or "\0" in value
    ):
        raise ValueError(
            f"{field} must be non-empty text within the "
            f"{LINKEDIN_TEXT_LIMIT}-character LinkedIn limit and contain no NUL"
        )
    return value


def _import_parts(
    entries: list[object], field: str, minimum: int, maximum: int
) -> list[tuple[int, str, str]]:
    parts = []
    seen = set()
    for index, entry in enumerate(entries):
        label = f"{field}[{index}]"
        if not isinstance(entry, dict):
            raise TypeError(f"{label} must be an object")
        part = entry.get("part")
        if type(part) is not int or not minimum <= part <= maximum or part in seen:
            raise ValueError(
                f"{label}.part must be unique and between {minimum} and {maximum}"
            )
        de = _import_text(entry.get("text_de"), f"{label}.text_de")
        ua = _import_text(entry.get("text_ua"), f"{label}.text_ua")
        seen.add(part)
        parts.append((part, de, ua))
    return parts


def _series_import(path: Path, settings: Settings) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise ValueError("could not read the import file") from None
    except (ValueError, RecursionError):
        raise ValueError("import file must contain valid UTF-8 JSON") from None
    if not isinstance(data, dict):
        raise TypeError("import file must contain a JSON object")
    repo = data.get("repo")
    if not isinstance(repo, str) or not repo.strip() or "\0" in repo:
        raise ValueError("repo must be a non-empty string without NUL")
    part, total_parts = data.get("part"), data.get("total_parts")
    if (
        type(part) is not int
        or type(total_parts) is not int
        or not 1 <= part <= total_parts <= 2**31 - 1
    ):
        raise ValueError(
            "part and total_parts must be integers with 1 <= part <= total_parts <= 2147483647"
        )
    started_on = _import_date(data.get("started_on"), "started_on")
    published_entries = _import_array(data, "published")
    published_texts = _import_parts(published_entries, "published", 1, part - 1)
    published = []
    for index, (entry, texts) in enumerate(
        zip(published_entries, published_texts, strict=True)
    ):
        assert isinstance(entry, dict)
        published.append(
            (
                *texts,
                _import_date(
                    entry.get("published_on"), f"published[{index}].published_on"
                ),
            )
        )
    planned = _import_parts(
        _import_array(data, "planned"), "planned", part, total_parts
    )
    posted_repos = []
    for name in _import_array(data, "posted_repos"):
        if not isinstance(name, str) or not name.strip() or "\0" in name:
            raise ValueError("posted_repos must contain non-empty strings without NUL")
        posted_repos.append(name.strip())
    repo = repo.strip()

    def import_into_database(_settings: Settings, conn: Connection[TupleRow]) -> None:
        SeriesStore(conn).import_series(
            repo=repo,
            total_parts=total_parts,
            part=part,
            started_on=started_on,
            published=published,
            planned=planned,
            posted_repos=posted_repos,
        )

    _with_database(import_into_database, settings)
    print(
        f"series-import: {repo} part {part}/{total_parts}, "
        f"{len(published)} published, {len(planned)} planned, {len(posted_repos)} posted repos"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="postpilot", description=f"postpilot {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command")
    for command in _COMMANDS:
        subparser = subparsers.add_parser(command)
        if command == "series-import":
            subparser.add_argument("path", type=Path)
        if command in {"render-video", "publish-reels"}:
            subparser.add_argument("--retry", action="store_true")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    try:
        settings = Settings.from_env(Path(".env"))
        if args.command == "render-video":
            tasks.validate_video_settings(settings)
    except ConfigError as error:
        parser.error(str(error))

    if args.command == "series-import":
        try:
            _series_import(args.path, settings)
        except (Error, UnicodeError):
            subparsers.choices[args.command].error("database import failed")
        except (TypeError, ValueError, ActiveSeriesExists) as error:
            subparsers.choices[args.command].error(str(error))
        return 0

    database_job = _DATABASE_COMMANDS.get(args.command)
    if args.command == "render-video":
        database_job = partial(tasks.render_video, retry=args.retry)
    elif args.command == "publish-reels":
        database_job = partial(tasks.publish_reels, retry=args.retry)
    assert database_job is not None
    operation = partial(_with_database, database_job, settings)
    if args.command == "poll-engagement":
        logging.basicConfig(format="%(message)s")
        logging.getLogger("postpilot.engagement.job").setLevel(logging.INFO)
    return run_job(args.command, operation, settings)


if __name__ == "__main__":
    raise SystemExit(main())
