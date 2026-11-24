from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from psycopg import Connection
from psycopg.rows import TupleRow


class VideoStatus(StrEnum):
    RENDERING = "RENDERING"
    READY = "READY"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class VideoRender:
    id: UUID
    item_id: UUID
    lang: str
    heygen_video_id: str | None
    status: VideoStatus
    script: str | None
    video: bytes | None = field(repr=False)
    duration: float | None
    requested_at: datetime
    approved_by: int | None
    approved_at: datetime | None
    linkedin: bool = False


class VideoStore:
    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn

    @contextmanager
    def render_lock(self) -> Iterator[bool]:
        # The 30-minute lease outlives the 25-minute workflow timeout.
        # Commit before rendering so checkpoints remain durable through the pooler.
        with self.conn.transaction():
            row = self.conn.execute(
                """
                INSERT INTO bot_state (key, value)
                VALUES ('video_render_lease', (clock_timestamp() + interval '30 minutes')::text)
                ON CONFLICT (key) DO UPDATE SET
                    value = (clock_timestamp() + interval '30 minutes')::text
                WHERE bot_state.value::timestamptz <= clock_timestamp()
                RETURNING value
                """
            ).fetchone()
        try:
            yield row is not None
        finally:
            if row is not None:
                with self.conn.transaction():
                    self.conn.execute(
                        "DELETE FROM bot_state WHERE key = 'video_render_lease' AND value = %s",
                        row,
                    )

    def rendering_ids(self) -> list[UUID]:
        with self.conn.transaction():
            rows = self.conn.execute(
                "SELECT id FROM video_renders WHERE lang = 'de' AND status = 'RENDERING'"
                " ORDER BY requested_at, id"
            ).fetchall()
        return [row[0] for row in rows]

    def claim(self, item_id: UUID, lang: str, *, retry: bool = False) -> UUID | None:
        # Give the retry a fresh ID so a late write from the first attempt cannot attach.
        with self.conn.transaction():
            row = self.conn.execute(
                """
                INSERT INTO video_renders (item_id, lang, status)
                VALUES (%s, %s, 'RENDERING')
                ON CONFLICT (item_id, lang) DO UPDATE SET
                    id = EXCLUDED.id, status = EXCLUDED.status,
                    requested_at = EXCLUDED.requested_at, retried = true,
                    heygen_video_id = NULL, script = NULL, video = NULL, duration = NULL,
                    linkedin = false
                WHERE %s AND video_renders.status = 'FAILED' AND NOT video_renders.retried
                RETURNING id
                """,
                (item_id, lang, retry),
            ).fetchone()
        return UUID(str(row[0])) if row is not None else None

    def attach(
        self,
        render_id: UUID,
        heygen_video_id: str,
        script: str,
        video: bytes,
        duration: float | None,
    ) -> bool:
        return self._update(
            """
            UPDATE video_renders SET status = 'READY', heygen_video_id = %s,
                script = %s, video = %s, duration = %s
            WHERE id = %s AND status = 'RENDERING'
            """,
            (heygen_video_id, script, video, duration, render_id),
        )

    def mark_failed(self, render_id: UUID, *, preview: bool = False) -> bool:
        return self._update(
            "UPDATE video_renders SET status = 'FAILED' WHERE id = %s AND status = %s",
            (render_id, "READY" if preview else "RENDERING"),
        )

    def count_this_month(self, now: datetime) -> int:
        start = now.astimezone(UTC).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        end = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else (start.replace(month=start.month + 1))
        )
        with self.conn.transaction():
            row = self.conn.execute(
                """
                SELECT count(*) FROM video_renders
                WHERE requested_at >= %s AND requested_at < %s AND status <> 'FAILED'
                """,
                (start, end),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def approve(self, render_id: UUID, user_id: int, now: datetime) -> bool:
        return self._update(
            """
            UPDATE video_renders SET status = 'APPROVED', approved_by = %s, approved_at = %s
            WHERE id = %s AND status = 'READY'
            """,
            (user_id, now, render_id),
        )

    def reject(self, render_id: UUID, user_id: int) -> bool:
        # The approval audit fields are reserved for successful approvals.
        return self._update(
            "UPDATE video_renders SET status = 'REJECTED' WHERE id = %s AND status = 'READY'",
            (render_id,),
        )

    def toggle_linkedin(self, render_id: UUID) -> bool | None:
        with self.conn.transaction():
            row = self.conn.execute(
                """
                UPDATE video_renders SET linkedin = NOT linkedin
                WHERE id = %s AND status = 'READY'
                RETURNING linkedin
                """,
                (render_id,),
            ).fetchone()
        return bool(row[0]) if row is not None else None

    def get(self, render_id: UUID) -> VideoRender | None:
        with self.conn.transaction():
            row = self.conn.execute(
                """
                SELECT id, item_id, lang, heygen_video_id, status, script, video,
                    duration, requested_at, approved_by, approved_at, linkedin
                FROM video_renders WHERE id = %s
                """,
                (render_id,),
            ).fetchone()
        if row is None:
            return None
        render_id, item_id, lang, remote_id, status, *details = row
        return VideoRender(
            render_id, item_id, lang, remote_id, VideoStatus(status), *details
        )

    def _update(self, query: str, params: tuple[object, ...]) -> bool:
        with self.conn.transaction():
            return self.conn.execute(query, params).rowcount == 1
