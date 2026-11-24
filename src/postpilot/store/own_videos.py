from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection, sql
from psycopg.rows import TupleRow


@dataclass
class OwnVideo:
    id: UUID
    item_id: UUID | None
    chat_id: int
    file_id: str
    duration: int | None
    ai_generated: bool
    interview: str
    status: str


class OwnVideoStore:
    """Operations for use inside the caller's transaction."""

    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn

    def claim_update(self, update_id: int) -> bool:
        return bool(
            self.conn.execute(
                "INSERT INTO own_video_updates VALUES (%s) ON CONFLICT DO NOTHING",
                (update_id,),
            ).rowcount
        )

    def add(
        self, chat_id: int, media: dict[str, Any], ai_generated: bool, interview: str
    ) -> UUID | None:
        id = uuid4()
        inserted = self.conn.execute(
            """
            INSERT INTO own_videos (id, chat_id, file_id, file_unique_id, size_bytes,
                duration, width, height, ai_generated, interview, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'collecting')
            ON CONFLICT (file_unique_id) DO NOTHING
            """,
            (
                id,
                chat_id,
                media["file_id"],
                media["file_unique_id"],
                media.get("file_size"),
                media.get("duration"),
                media.get("width"),
                media.get("height"),
                ai_generated,
                interview,
            ),
        )
        return id if inserted.rowcount else None

    def get(self, chat_id: int, **key: UUID | int) -> OwnVideo | None:
        ((column, value),) = key.items()
        row = self.conn.execute(
            sql.SQL(
                "SELECT id, item_id, chat_id, file_id, duration, ai_generated,"
                " interview, status FROM own_videos WHERE chat_id = %s AND {} = %s FOR UPDATE"
            ).format(sql.Identifier(column)),
            (chat_id, value),
        ).fetchone()
        return OwnVideo(*row) if row else None

    def get_by_item(self, item_id: UUID) -> OwnVideo | None:
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT id, item_id, chat_id, file_id, duration, ai_generated,"
                " interview, status FROM own_videos WHERE item_id = %s",
                (item_id,),
            ).fetchone()
        return OwnVideo(*row) if row else None

    def set_prompt(self, id: UUID, message_id: int) -> None:
        self.conn.execute(
            "UPDATE own_videos SET prompt_message_id = %s WHERE id = %s",
            (message_id, id),
        )

    def set_interview(self, id: UUID, interview: str) -> None:
        self.conn.execute(
            "UPDATE own_videos SET interview = %s WHERE id = %s", (interview, id)
        )

    def finish(self, id: UUID, item_id: UUID | None = None) -> None:
        self.conn.execute(
            "UPDATE own_videos SET status = %s, item_id = %s WHERE id = %s",
            ("generated" if item_id else "skipped", item_id, id),
        )
