from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from psycopg import Connection
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from postpilot.core.models import Item
from postpilot.core.states import ItemState
from postpilot.store.repo import ItemRepo

_COLUMNS = "week, item_id, repo, series_id, part, facts, outcome, recorded_at"


@dataclass(frozen=True)
class WeeklyDraft:
    week: date
    item_id: UUID | None
    repo: str | None
    series_id: UUID | None
    part: int | None
    facts: dict[str, object]
    outcome: str
    recorded_at: datetime | None


class WeeklyStore:
    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn

    def get(self, week: date, *, lock: bool = False) -> WeeklyDraft | None:
        suffix = " FOR UPDATE" if lock else ""
        with self.conn.transaction():
            row = self.conn.execute(
                f"SELECT {_COLUMNS} FROM weekly_drafts WHERE week = %s{suffix}", (week,)
            ).fetchone()
        return WeeklyDraft(*row) if row else None

    def create_drafted(
        self,
        week: date,
        repo: str,
        series_id: UUID | None,
        part: int | None,
        facts: dict[str, object],
        de: str,
        ua: str,
    ) -> Item:
        items = ItemRepo(self.conn)
        with self.conn.transaction():
            item = items.create_item("github")
            items.add_variant(item.id, "de", de)
            items.add_variant(item.id, "ua", ua)
            self.conn.execute(
                """
                INSERT INTO weekly_drafts (week, item_id, repo, series_id, part, facts, outcome)
                VALUES (%s, %s, %s, %s, %s, %s, 'drafted')
                """,
                (week, item.id, repo, series_id, part, Jsonb(facts)),
            )
            return items.transition(item.id, ItemState.AWAITING_APPROVAL)

    def record_outcome(
        self,
        week: date,
        outcome: Literal["skipped", "no_activity"],
        repo: str | None,
        facts: dict[str, object],
    ) -> None:
        with self.conn.transaction():
            self.conn.execute(
                "INSERT INTO weekly_drafts (week, outcome, repo, facts) VALUES (%s, %s, %s, %s)",
                (week, outcome, repo, Jsonb(facts)),
            )

    def expire_older(self, item_id: UUID) -> None:
        items = ItemRepo(self.conn)
        with self.conn.transaction():
            rows = self.conn.execute(
                """
                SELECT older.id FROM items AS older JOIN items AS new ON new.id = %s
                WHERE older.source = 'github' AND older.state = 'AWAITING_APPROVAL'
                    AND older.id <> new.id AND older.created_at <= new.created_at
                ORDER BY older.created_at, older.id FOR UPDATE OF older
                """,
                (item_id,),
            ).fetchall()
            for (older_id,) in rows:
                items.transition(older_id, ItemState.SKIPPED)

    def list_unrecorded_published(self) -> list[WeeklyDraft]:
        with self.conn.transaction():
            rows = self.conn.execute(
                f"""
                SELECT {_COLUMNS} FROM weekly_drafts
                WHERE recorded_at IS NULL AND item_id IN (SELECT id FROM items WHERE state = 'PUBLISHED')
                ORDER BY week
                """
            ).fetchall()
        return [WeeklyDraft(*row) for row in rows]

    def mark_recorded(self, week: date) -> None:
        with self.conn.transaction():
            self.conn.execute(
                "UPDATE weekly_drafts SET recorded_at = now() WHERE week = %s AND recorded_at IS NULL",
                (week,),
            )
