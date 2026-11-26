import json
from dataclasses import asdict, dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

from psycopg import Connection, sql
from psycopg.rows import TupleRow

from postpilot.core.limits import REELS_MAX_DURATION, REELS_MIN_DURATION


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
    width: int | None = None
    height: int | None = None

    @property
    def reels_eligible(self) -> bool:
        return (
            self.duration is None
            or REELS_MIN_DURATION <= self.duration <= REELS_MAX_DURATION
        )

    @property
    def has_description(self) -> bool:
        # Both described intake paths require a nonempty interview to create an item.
        return bool(self.interview)


IntakeStep = Literal["kind", "video", "description", "text", "closed"]


@dataclass
class OwnVideoIntake:
    token: str
    step: IntakeStep
    updated_at: float
    ai_generated: bool = False
    video_id: str | None = None


class OwnVideoStore:
    """Operations for use inside the caller's transaction."""

    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn

    def lock_intake(self, chat_id: int) -> None:
        # Lock even when the chat has no state row yet.
        self.conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext(%s))",
            (f"own_intake:{chat_id}",),
        )

    def get_intake(self, chat_id: int) -> OwnVideoIntake | None:
        row = self.conn.execute(
            "SELECT value FROM bot_state WHERE key = %s", (f"own_intake:{chat_id}",)
        ).fetchone()
        return OwnVideoIntake(**json.loads(row[0])) if row else None

    def save_intake(self, chat_id: int, intake: OwnVideoIntake) -> None:
        self.conn.execute(
            """
            INSERT INTO bot_state (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (f"own_intake:{chat_id}", json.dumps(asdict(intake))),
        )

    def has_update(self, update_id: int) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM own_video_updates WHERE update_id = %s", (update_id,)
            ).fetchone()
            is not None
        )

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
            ON CONFLICT (file_unique_id) DO UPDATE SET
                id = EXCLUDED.id, item_id = NULL, chat_id = EXCLUDED.chat_id,
                prompt_message_id = NULL, file_id = EXCLUDED.file_id,
                size_bytes = EXCLUDED.size_bytes, duration = EXCLUDED.duration,
                width = EXCLUDED.width, height = EXCLUDED.height,
                ai_generated = EXCLUDED.ai_generated, interview = EXCLUDED.interview,
                status = 'collecting', created_at = now()
            -- A skipped or cancelled video was never posted, so it may be sent again.
            WHERE own_videos.status = 'skipped'
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
                " interview, status, width, height FROM own_videos WHERE chat_id = %s AND {} = %s FOR UPDATE"
            ).format(sql.Identifier(column)),
            (chat_id, value),
        ).fetchone()
        return OwnVideo(*row) if row else None

    def get_by_item(self, item_id: UUID) -> OwnVideo | None:
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT id, item_id, chat_id, file_id, duration, ai_generated,"
                " interview, status, width, height FROM own_videos WHERE item_id = %s",
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
