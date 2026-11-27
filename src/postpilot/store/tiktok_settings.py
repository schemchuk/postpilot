from datetime import datetime
from uuid import UUID

from psycopg import Connection
from psycopg.rows import TupleRow

from postpilot.core.tiktok import TikTokChoice


class TikTokSettingsStore:
    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn

    def get(self, item_id: UUID, by: datetime | None = None) -> TikTokChoice | None:
        with self.conn.transaction():
            row = self.conn.execute(
                """
                SELECT privacy_level, allow_comment, allow_duet, allow_stitch
                FROM tiktok_settings
                WHERE item_id = %s AND (%s::timestamptz IS NULL OR chosen_at <= %s)
                """,
                (item_id, by, by),
            ).fetchone()
        return TikTokChoice(*row) if row else None

    def save(self, item_id: UUID, choice: TikTokChoice) -> None:
        with self.conn.transaction():
            self.conn.execute(
                """
                INSERT INTO tiktok_settings
                    (item_id, privacy_level, allow_comment, allow_duet, allow_stitch)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (item_id) DO UPDATE SET
                    privacy_level = EXCLUDED.privacy_level,
                    allow_comment = EXCLUDED.allow_comment,
                    allow_duet = EXCLUDED.allow_duet,
                    allow_stitch = EXCLUDED.allow_stitch,
                    chosen_at = EXCLUDED.chosen_at
                """,
                (
                    item_id,
                    choice.privacy_level,
                    choice.allow_comment,
                    choice.allow_duet,
                    choice.allow_stitch,
                ),
            )
