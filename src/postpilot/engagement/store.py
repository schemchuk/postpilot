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
    reply_status: str | None = None
    reply_container_id: str | None = None
    reply_remote_id: str | None = None
    reply_sent_at: datetime | None = None
    reply_claimed_at: datetime | None = None


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
        *,
        notified: bool = False,
    ) -> EngagementEvent | None:
        with (
            self.conn.transaction(),
            self.conn.cursor(row_factory=class_row(EngagementEvent)) as cur,
        ):
            return cur.execute(
                """
                INSERT INTO engagement_events
                    (id, channel, kind, source_ref, actor, text, post_url,
                     reply_draft, occurred_at, created_at, notified_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(),
                        CASE WHEN %s THEN now() END)
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
                    notified,
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

    def find_by_message_id(self, message_id: int) -> EngagementEvent | None:
        with (
            self.conn.transaction(),
            self.conn.cursor(row_factory=class_row(EngagementEvent)) as cur,
        ):
            return cur.execute(
                "SELECT * FROM engagement_events WHERE kind = 'comment'"
                " AND telegram_message_id = %s",
                (message_id,),
            ).fetchone()

    def set_reply_draft(self, id: UUID, text: str) -> bool:
        with self.conn.transaction():
            return (
                self.conn.execute(
                    """
                UPDATE engagement_events SET reply_draft = %s,
                    reply_container_id = CASE WHEN reply_draft = %s THEN reply_container_id END
                WHERE id = %s AND (reply_status IS NULL OR reply_status = 'FAILED')
                """,
                    (text, text, id),
                ).rowcount
                == 1
            )

    def claim_reply(
        self, id: UUID, now: datetime, stale_after: timedelta = timedelta(minutes=5)
    ) -> EngagementEvent | None:
        with (
            self.conn.transaction(),
            self.conn.cursor(row_factory=class_row(EngagementEvent)) as cur,
        ):
            return cur.execute(
                """
                UPDATE engagement_events SET reply_status = 'SENDING', reply_claimed_at = %s
                WHERE id = %s AND channel = 'threads' AND kind = 'comment'
                    AND (reply_status IS NULL OR reply_status = 'FAILED'
                        OR (reply_status = 'SENDING' AND reply_claimed_at < %s))
                RETURNING *
                """,
                (now, id, now - stale_after),
            ).fetchone()

    def save_reply_container(self, id: UUID, container_id: str) -> None:
        with self.conn.transaction():
            self.conn.execute(
                "UPDATE engagement_events SET reply_container_id = %s"
                " WHERE id = %s AND reply_status = 'SENDING'",
                (container_id, id),
            )

    def mark_reply_sent(self, id: UUID, remote_id: str | None, now: datetime) -> None:
        with self.conn.transaction():
            self.conn.execute(
                "UPDATE engagement_events SET reply_status = 'SENT',"
                " reply_remote_id = %s, reply_sent_at = %s"
                " WHERE id = %s AND reply_status = 'SENDING'",
                (remote_id, now, id),
            )

    def mark_reply_failed(self, id: UUID) -> None:
        with self.conn.transaction():
            self.conn.execute(
                "UPDATE engagement_events SET reply_status = 'FAILED'"
                " WHERE id = %s AND reply_status = 'SENDING'",
                (id,),
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
