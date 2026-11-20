import json
from collections.abc import Callable
from datetime import date
from typing import TypedDict, cast
from uuid import UUID

from psycopg import Connection
from psycopg.rows import TupleRow

from postpilot.core.models import Language


class GitHubWatchCounts(TypedDict):
    stars: int
    forks: int


class BotState:
    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn

    def get_imap_cursor(self) -> tuple[int, int] | None:
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT value FROM bot_state WHERE key = 'linkedin_imap_cursor'"
            ).fetchone()
            if row is None:
                return None
            validity, uid = row[0].split(":")
            return int(validity), int(uid)

    def set_imap_cursor(self, validity: int, uid: int) -> None:
        with self.conn.transaction():
            self.conn.execute(
                """
                INSERT INTO bot_state (key, value) VALUES ('linkedin_imap_cursor', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                (f"{validity}:{uid}",),
            )

    def get_github_watch(self) -> dict[str, GitHubWatchCounts]:
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT value FROM bot_state WHERE key = 'github_watch'"
            ).fetchone()
        return cast(dict[str, GitHubWatchCounts], json.loads(row[0])) if row else {}

    def set_github_watch(self, repo_id: str, counts: GitHubWatchCounts) -> None:
        with self.conn.transaction():
            self.conn.execute(
                """
                INSERT INTO bot_state (key, value) VALUES ('github_watch', %s)
                ON CONFLICT (key) DO UPDATE SET value =
                    (bot_state.value::jsonb || EXCLUDED.value::jsonb)::text
                """,
                (json.dumps({repo_id: counts}),),
            )

    def get_offset(self) -> int:
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT value FROM bot_state WHERE key = 'telegram_offset'"
            ).fetchone()
            return int(row[0]) if row else 0

    def set_offset(self, offset: int) -> None:
        with self.conn.transaction():
            self.conn.execute(
                """
                INSERT INTO bot_state (key, value) VALUES ('telegram_offset', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                WHERE bot_state.value::bigint < EXCLUDED.value::bigint
                """,
                (str(offset),),
            )

    def set_pending_edit(self, user_id: int, item_id: UUID, lang: Language) -> None:
        with self.conn.transaction():
            self.conn.execute(
                """
                INSERT INTO pending_edits (user_id, item_id, lang) VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET item_id = EXCLUDED.item_id,
                    lang = EXCLUDED.lang, created_at = now()
                """,
                (user_id, item_id, lang),
            )

    def take_pending_edit(self, user_id: int) -> tuple[UUID, Language] | None:
        with self.conn.transaction():
            row = self.conn.execute(
                "DELETE FROM pending_edits WHERE user_id = %s RETURNING item_id, lang",
                (user_id,),
            ).fetchone()
            return cast(tuple[UUID, Language], row) if row else None

    def send_token_alert_once(
        self, provider: str, day: date, send: Callable[[], None]
    ) -> None:
        self._send_daily_once(f"token_alert:{provider}", day, send)

    def send_renewal_prompt_once(
        self, provider: str, day: date, send: Callable[[], None]
    ) -> None:
        self._send_daily_once(f"token_renewal:{provider}", day, send)

    def _send_daily_once(self, key: str, day: date, send: Callable[[], None]) -> None:
        with self.conn.transaction():
            result = self.conn.execute(
                """
                INSERT INTO bot_state (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                WHERE bot_state.value < EXCLUDED.value
                """,
                (key, day.isoformat()),
            )
            if result.rowcount:
                send()

    def save_oauth_nonce(self, nonce: str) -> None:
        with self.conn.transaction():
            self.conn.execute(
                "INSERT INTO bot_state (key, value) VALUES (%s, 'issued')",
                (f"oauth_state:{nonce}",),
            )

    def consume_oauth_nonce(self, nonce: str) -> bool:
        with self.conn.transaction():
            row = self.conn.execute(
                "DELETE FROM bot_state WHERE key = %s RETURNING key",
                (f"oauth_state:{nonce}",),
            ).fetchone()
        return row is not None
