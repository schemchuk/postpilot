from typing import Any, cast

from psycopg import Connection
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

PENDING_BATCH_SIZE = 100


class UpdateInbox:
    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn

    def insert(self, update: dict[str, Any]) -> bool:
        with self.conn.transaction():
            result = self.conn.execute(
                """
                INSERT INTO telegram_inbox (update_id, payload) VALUES (%s, %s)
                ON CONFLICT (update_id) DO NOTHING
                """,
                (update["update_id"], Jsonb(update)),
            )
            return result.rowcount == 1

    def pending(
        self, limit: int = PENDING_BATCH_SIZE, *, after_update_id: int | None = None
    ) -> list[tuple[int, dict[str, Any]]]:
        with self.conn.transaction():
            rows = self.conn.execute(
                """
                SELECT update_id, payload FROM telegram_inbox
                WHERE processed_at IS NULL
                    AND (%s::bigint IS NULL OR update_id > %s)
                ORDER BY update_id LIMIT %s
                """,
                (after_update_id, after_update_id, limit),
            ).fetchall()
            return cast(list[tuple[int, dict[str, Any]]], rows)

    def mark_processed(self, update_id: int) -> bool:
        with self.conn.transaction():
            result = self.conn.execute(
                """
                UPDATE telegram_inbox SET processed_at = now()
                WHERE update_id = %s AND processed_at IS NULL
                """,
                (update_id,),
            )
            return result.rowcount == 1

    def record_failure(self, update_id: int) -> bool:
        with self.conn.transaction():
            row = self.conn.execute(
                """
                UPDATE telegram_inbox SET attempts = attempts + 1,
                    processed_at = CASE WHEN attempts + 1 >= 3 THEN now() END
                WHERE update_id = %s AND processed_at IS NULL
                RETURNING processed_at IS NOT NULL
                """,
                (update_id,),
            ).fetchone()
            return bool(row and row[0])

    def purge(self, days: int = 7) -> int:
        with self.conn.transaction():
            result = self.conn.execute(
                "DELETE FROM telegram_inbox WHERE processed_at < now() - make_interval(days => %s)",
                (days,),
            )
            return result.rowcount
