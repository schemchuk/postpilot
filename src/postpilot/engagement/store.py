from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import TupleRow, class_row


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
                "SELECT * FROM engagement_events WHERE notified_at IS NULL ORDER BY created_at, id"
            ).fetchall()

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
