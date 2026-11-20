from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import TupleRow, class_row

PERSONAL_DATA_RETENTION = timedelta(days=90)


@dataclass(frozen=True)
class EngagementEvent:
    id: UUID
    channel: str
    kind: str
    source_ref: str
    actor: str | None
    text: str | None
    post_url: str | None
    reply_draft: str | None
    occurred_at: datetime
    notified_at: datetime | None
    telegram_message_id: int | None
    created_at: datetime


class EngagementStore:
    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn

    def insert_if_new(
        self,
        channel: str,
        kind: str,
        source_ref: str,
        actor: str | None,
        text: str | None,
        post_url: str | None,
        reply_draft: str | None,
        occurred_at: datetime,
    ) -> EngagementEvent | None:
        with (
            self.conn.transaction(),
            self.conn.cursor(row_factory=class_row(EngagementEvent)) as cur,
        ):
            return cur.execute(
                """
                INSERT INTO engagement_events
                    (id, channel, kind, source_ref, actor, text, post_url,
                     reply_draft, occurred_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (channel, source_ref) DO NOTHING RETURNING *
                """,
                (
                    uuid4(),
                    channel,
                    kind,
                    source_ref,
                    actor,
                    text,
                    post_url,
                    reply_draft,
                    occurred_at,
                ),
            ).fetchone()

    def list_unnotified(self) -> list[EngagementEvent]:
        with (
            self.conn.transaction(),
            self.conn.cursor(row_factory=class_row(EngagementEvent)) as cur,
        ):
            return cur.execute(
                "SELECT * FROM engagement_events WHERE notified_at IS NULL"
                " AND occurred_at >= %s ORDER BY created_at, id",
                (datetime.now(UTC) - PERSONAL_DATA_RETENTION,),
            ).fetchall()

    def purge_personal_data(self, now: datetime) -> int:
        with self.conn.transaction():
            return self.conn.execute(
                """
                UPDATE engagement_events
                SET text = NULL, actor = NULL, reply_draft = NULL
                WHERE occurred_at < %s
                    AND (text IS NOT NULL OR actor IS NOT NULL OR reply_draft IS NOT NULL)
                """,
                (now - PERSONAL_DATA_RETENTION,),
            ).rowcount

    def mark_notified(self, id: UUID, message_id: int | None) -> None:
        with self.conn.transaction():
            self.conn.execute(
                "UPDATE engagement_events SET notified_at = now(), telegram_message_id = %s"
                " WHERE id = %s AND notified_at IS NULL",
                (message_id, id),
            )

    def get(self, id: UUID) -> EngagementEvent | None:
        with (
            self.conn.transaction(),
            self.conn.cursor(row_factory=class_row(EngagementEvent)) as cur,
        ):
            return cur.execute(
                "SELECT * FROM engagement_events WHERE id = %s", (id,)
            ).fetchone()

    def set_reply_draft(self, id: UUID, text: str) -> None:
        with self.conn.transaction():
            self.conn.execute(
                "UPDATE engagement_events SET reply_draft = %s WHERE id = %s",
                (text, id),
            )

    def recent_threads_posts(self, since: datetime) -> list[str]:
        with self.conn.transaction():
            rows = self.conn.execute(
                """
                SELECT remote_id FROM publications
                WHERE channel = 'threads' AND status = 'PUBLISHED'
                    AND remote_id IS NOT NULL AND updated_at >= %s
                ORDER BY updated_at DESC, item_id
                """,
                (since,),
            ).fetchall()
        return [remote_id for (remote_id,) in rows]

    def known_refs(self, channel: str, refs: list[str]) -> set[str]:
        if not refs:
            return set()
        with self.conn.transaction():
            rows = self.conn.execute(
                "SELECT source_ref FROM engagement_events"
                " WHERE channel = %s AND source_ref = ANY(%s)",
                (channel, refs),
            ).fetchall()
        return {ref for (ref,) in rows}
