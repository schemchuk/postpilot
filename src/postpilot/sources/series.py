from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import TupleRow

_COLUMNS = "id, repo, part, total_parts, started_on, status"


@dataclass(frozen=True)
class Series:
    id: UUID
    repo: str
    part: int
    total_parts: int | None
    started_on: date
    status: Literal["active", "queued", "stopped", "completed"]


@dataclass(frozen=True)
class SeriesPart:
    series_id: UUID
    part: int
    text_de: str
    text_ua: str
    published_on: date


class ActiveSeriesExists(RuntimeError):
    """The current series must be stopped before starting a new one."""


class QueuedSeriesExists(RuntimeError):
    """Only one series can wait behind the active one."""


class NoActiveSeries(RuntimeError):
    """A series can be queued only behind an active one."""


class SeriesStore:
    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn

    def get_active(self) -> Series | None:
        with self.conn.transaction():
            row = self.conn.execute(
                f"SELECT {_COLUMNS} FROM series WHERE status = 'active'"
            ).fetchone()
        return Series(*row) if row else None

    def get_queued(self) -> Series | None:
        with self.conn.transaction():
            row = self.conn.execute(
                f"SELECT {_COLUMNS} FROM series WHERE status = 'queued'"
            ).fetchone()
        return Series(*row) if row else None

    def queue(self, repo: str, total_parts: int | None) -> Series:
        if total_parts is not None and total_parts < 1:
            raise ValueError("total_parts must be positive or None.")
        with self.conn.transaction():
            # Locking the active row orders this insert against a concurrent
            # completion, so the queued row is never left behind without one.
            if (
                self.conn.execute(
                    "SELECT 1 FROM series WHERE status = 'active' FOR UPDATE"
                ).fetchone()
                is None
            ):
                raise NoActiveSeries("No series is active.")
            row = self.conn.execute(
                f"""
                INSERT INTO series (id, repo, total_parts, status)
                VALUES (%s, %s, %s, 'queued')
                ON CONFLICT (status) WHERE status = 'queued' DO NOTHING
                RETURNING {_COLUMNS}
                """,
                (uuid4(), repo, total_parts),
            ).fetchone()
            if row is None:
                raise QueuedSeriesExists("A series is already queued.")
        return Series(*row)

    def cancel_queued(self) -> Series | None:
        with self.conn.transaction():
            row = self.conn.execute(
                f"DELETE FROM series WHERE status = 'queued' RETURNING {_COLUMNS}"
            ).fetchone()
        return Series(*row) if row else None

    def _promote_queued(self) -> None:
        self.conn.execute(
            """
            UPDATE series SET status = 'active', part = 1, started_on = CURRENT_DATE
            WHERE status = 'queued'
            """
        )

    def start(
        self,
        repo: str,
        total_parts: int | None,
        *,
        part: int = 1,
        started_on: date | None = None,
    ) -> Series:
        if total_parts is not None and total_parts < 1:
            raise ValueError("total_parts must be positive or None.")
        with self.conn.transaction():
            row = self.conn.execute(
                f"""
                INSERT INTO series (id, repo, total_parts, part, started_on)
                VALUES (%s, %s, %s, %s, COALESCE(%s, CURRENT_DATE))
                ON CONFLICT (status) WHERE status = 'active' DO NOTHING
                RETURNING {_COLUMNS}
                """,
                (uuid4(), repo, total_parts, part, started_on),
            ).fetchone()
            if row is None:
                raise ActiveSeriesExists("A series is already active.")
        return Series(*row)

    def import_series(
        self,
        *,
        repo: str,
        total_parts: int,
        part: int,
        started_on: date,
        published: Sequence[tuple[int, str, str, date]],
        planned: Sequence[tuple[int, str, str]],
        posted_repos: Sequence[str],
    ) -> Series:
        with self.conn.transaction():
            series = self.start(repo, total_parts, part=part, started_on=started_on)
            with self.conn.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO series_parts (series_id, part, text_de, text_ua, published_on)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    [(series.id, *entry) for entry in published],
                )
                cursor.executemany(
                    """
                    INSERT INTO series_planned_parts (series_id, part, text_de, text_ua)
                    VALUES (%s, %s, %s, %s)
                    """,
                    [(series.id, *entry) for entry in planned],
                )
            for posted in posted_repos:
                self.mark_posted(posted)
        return series

    def planned_part(self, series: Series) -> tuple[str, str] | None:
        with self.conn.transaction():
            row = self.conn.execute(
                """
                SELECT text_de, text_ua FROM series_planned_parts
                WHERE series_id = %s AND part = %s
                """,
                (series.id, series.part),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def stop(self) -> Series | None:
        with self.conn.transaction():
            row = self.conn.execute(
                f"""
                UPDATE series SET status = 'stopped' WHERE status = 'active'
                RETURNING {_COLUMNS}
                """
            ).fetchone()
            if row is not None:
                self._promote_queued()
        return Series(*row) if row else None

    def complete(self, *, series_id: UUID | None = None) -> Series | None:
        with self.conn.transaction():
            row = self.conn.execute(
                f"""
                UPDATE series SET status = 'completed' WHERE status = 'active'
                    AND (%s::uuid IS NULL OR id = %s)
                RETURNING {_COLUMNS}
                """,
                (series_id, series_id),
            ).fetchone()
            if row is not None:
                self._promote_queued()
        return Series(*row) if row else None

    def record_part(
        self,
        repo: str,
        part: int,
        text_de: str,
        text_ua: str,
        *,
        series_id: UUID | None = None,
    ) -> Series | None:
        with self.conn.transaction():
            row = self.conn.execute(
                f"""
                UPDATE series
                SET status = CASE WHEN part >= total_parts THEN 'completed' ELSE status END,
                    part = CASE WHEN part >= total_parts THEN part ELSE part + 1 END
                WHERE status = 'active' AND lower(repo) = lower(%s) AND part = %s
                    AND (%s::uuid IS NULL OR id = %s)
                RETURNING {_COLUMNS}
                """,
                (repo, part, series_id, series_id),
            ).fetchone()
            if row is None:
                return None
            series = Series(*row)
            self.conn.execute(
                """
                INSERT INTO series_parts (series_id, part, text_de, text_ua)
                VALUES (%s, %s, %s, %s)
                """,
                (series.id, part, text_de, text_ua),
            )
            self.mark_posted(series.repo)
            if series.status == "completed":
                self._promote_queued()
        return series

    def history(self, series: Series) -> list[SeriesPart]:
        with self.conn.transaction():
            rows = self.conn.execute(
                """
                SELECT series_id, part, text_de, text_ua, published_on
                FROM series_parts WHERE series_id = %s ORDER BY part
                """,
                (series.id,),
            ).fetchall()
        return [SeriesPart(*row) for row in rows]

    def mark_posted(self, repo: str) -> None:
        with self.conn.transaction():
            self.conn.execute(
                "INSERT INTO posted_repos (repo) VALUES (%s) ON CONFLICT DO NOTHING",
                (repo.casefold(),),
            )

    def posted_repos(self) -> set[str]:
        with self.conn.transaction():
            rows = self.conn.execute("SELECT repo FROM posted_repos").fetchall()
        return {repo for (repo,) in rows}
