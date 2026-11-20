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
    status: Literal["active", "stopped", "completed"]


@dataclass(frozen=True)
class SeriesPart:
    series_id: UUID
    part: int
    text_de: str
    text_ua: str
    published_on: date


class ActiveSeriesExists(RuntimeError):
    """The current series must be stopped before starting a new one."""


class SeriesStore:
    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn

    def get_active(self) -> Series | None:
        with self.conn.transaction():
            row = self.conn.execute(
                f"SELECT {_COLUMNS} FROM series WHERE status = 'active'"
            ).fetchone()
        return Series(*row) if row else None

    def start(self, repo: str, total_parts: int | None) -> Series:
        if total_parts is not None and total_parts < 1:
            raise ValueError("total_parts must be positive or None.")
        with self.conn.transaction():
            row = self.conn.execute(
                f"""
                INSERT INTO series (id, repo, total_parts) VALUES (%s, %s, %s)
                ON CONFLICT (status) WHERE status = 'active' DO NOTHING
                RETURNING {_COLUMNS}
                """,
                (uuid4(), repo, total_parts),
            ).fetchone()
            if row is None:
                raise ActiveSeriesExists("A series is already active.")
        return Series(*row)

    def stop(self) -> Series | None:
        with self.conn.transaction():
            row = self.conn.execute(
                f"""
                UPDATE series SET status = 'stopped' WHERE status = 'active'
                RETURNING {_COLUMNS}
                """
            ).fetchone()
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
