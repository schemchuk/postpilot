from datetime import datetime
from uuid import UUID

from psycopg import Connection, sql
from psycopg.rows import TupleRow

from postpilot.core.tiktok import TikTokChoice, TikTokSelection, TikTokToggle


class TikTokSettingsStore:
    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn

    def get(self, item_id: UUID, by: datetime | None = None) -> TikTokChoice | None:
        state = self.get_partial(item_id, by)
        if state.privacy_level is None:
            return None
        return TikTokChoice(
            state.privacy_level,
            state.allow_comment,
            state.allow_duet,
            state.allow_stitch,
        )

    def get_partial(self, item_id: UUID, by: datetime | None = None) -> TikTokSelection:
        with self.conn.transaction():
            row = self.conn.execute(
                """
                SELECT privacy_level, allow_comment, allow_duet, allow_stitch
                FROM tiktok_settings
                WHERE item_id = %s AND (%s::timestamptz IS NULL OR chosen_at <= %s)
                """,
                (item_id, by, by),
            ).fetchone()
        return TikTokSelection(*row) if row else TikTokSelection()

    def set_privacy(self, item_id: UUID, privacy: str, at: datetime) -> None:
        self._change(item_id, "privacy_level", privacy, at)

    def flip_toggle(self, item_id: UUID, toggle: TikTokToggle, at: datetime) -> None:
        self._change(item_id, f"allow_{toggle}", None, at)

    def _change(
        self, item_id: UUID, column: str, privacy: str | None, at: datetime
    ) -> None:
        field = sql.Identifier(column)
        value = (
            sql.SQL("EXCLUDED.privacy_level")
            if privacy is not None
            else sql.SQL("NOT tiktok_settings.{}").format(field)
        )
        with self.conn.transaction():
            self.conn.execute(
                sql.SQL("""
                INSERT INTO tiktok_settings (item_id, {field}, chosen_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (item_id) DO UPDATE SET
                    {field} = {value}, chosen_at = EXCLUDED.chosen_at
                """).format(field=field, value=value),
                (item_id, privacy if privacy is not None else True, at),
            )

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
