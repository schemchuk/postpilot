from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from psycopg import Connection
from psycopg.rows import TupleRow

from postpilot.store.repo import LostPublicationClaim


@dataclass(frozen=True)
class VideoClaim:
    render_id: UUID
    channel: str
    updated_at: datetime
    remote_ref: str | None
    recovered: bool
    stale: bool

    def key(self) -> tuple[UUID, str, datetime]:
        return self.render_id, self.channel, self.updated_at


class VideoPublicationStore:
    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn

    def oldest_eligible(
        self, channels: set[str], stale_after: timedelta, *, retry: bool = False
    ) -> UUID | None:
        with self.conn.transaction():
            row = self.conn.execute(
                """
                SELECT id FROM video_renders r
                WHERE lang = 'de' AND status = 'APPROVED' AND EXISTS (
                    SELECT 1 FROM unnest(%s::text[]) AS configured(channel)
                    LEFT JOIN video_publications p
                        ON p.render_id = r.id AND p.channel = configured.channel
                    WHERE p.status IS NULL OR (%s AND p.status = 'FAILED')
                        OR (p.status = 'PUBLISHING'
                            AND p.updated_at < clock_timestamp() - %s::interval)
                )
                ORDER BY requested_at, id LIMIT 1
                """,
                (sorted(channels), retry, stale_after),
            ).fetchone()
        return UUID(str(row[0])) if row else None

    def claim(
        self,
        render_id: UUID,
        channel: str,
        stale_after: timedelta,
        *,
        retry: bool = False,
    ) -> VideoClaim | None:
        with self.conn.transaction():
            render = self.conn.execute(
                "SELECT status FROM video_renders WHERE id = %s FOR UPDATE",
                (render_id,),
            ).fetchone()
            if render is None or render[0] != "APPROVED":
                return None
            previous = self.conn.execute(
                "SELECT status FROM video_publications WHERE render_id = %s AND channel = %s FOR UPDATE",
                (render_id, channel),
            ).fetchone()
            # Retain checkpoints on failed retries too. A rejected status lookup
            # cannot prove that an earlier finish did not publish.
            row = self.conn.execute(
                """
                INSERT INTO video_publications AS p (render_id, channel, status, updated_at)
                VALUES (%s, %s, 'PUBLISHING', clock_timestamp())
                ON CONFLICT (render_id, channel) DO UPDATE SET
                    status = 'PUBLISHING', error = NULL, remote_id = NULL,
                    updated_at = clock_timestamp()
                WHERE (%s AND p.status = 'FAILED') OR (p.status = 'PUBLISHING'
                    AND p.updated_at < clock_timestamp() - %s::interval)
                RETURNING updated_at, remote_ref
                """,
                (render_id, channel, retry, stale_after),
            ).fetchone()
        if row is None:
            return None
        return VideoClaim(
            render_id,
            channel,
            row[0],
            row[1],
            previous is not None,
            previous is not None and previous[0] == "PUBLISHING",
        )

    def save_ref(self, claim: VideoClaim, remote_ref: str) -> None:
        self._update(
            claim,
            "remote_ref_claimed_at = CASE WHEN remote_ref = %s "
            "THEN remote_ref_claimed_at ELSE updated_at END, remote_ref = %s",
            (remote_ref, remote_ref),
        )

    def mark_published(self, claim: VideoClaim, remote_id: str) -> None:
        self._update(
            claim,
            "status = 'PUBLISHED', remote_id = %s, updated_at = now()",
            (remote_id,),
        )

    def mark_failed(self, claim: VideoClaim, error: str) -> None:
        self._update(
            claim, "status = 'FAILED', error = %s, updated_at = now()", (error,)
        )

    def release(self, claim: VideoClaim) -> None:
        with self.conn.transaction():
            deleted = self.conn.execute(
                """
                DELETE FROM video_publications WHERE render_id = %s AND channel = %s
                    AND updated_at = %s AND status = 'PUBLISHING' AND remote_ref IS NULL
                """,
                claim.key(),
            ).rowcount
            if not deleted:
                # The next worker must recover a saved session even after a token hold.
                self._update(claim, "updated_at = '-infinity'")

    def _update(
        self, claim: VideoClaim, assignments: str, values: tuple[str, ...] = ()
    ) -> None:
        with self.conn.transaction():
            result = self.conn.execute(
                f"""
                UPDATE video_publications SET {assignments}
                WHERE render_id = %s AND channel = %s
                    AND updated_at = %s AND status = 'PUBLISHING'
                """,
                (*values, *claim.key()),
            )
            if result.rowcount != 1:
                raise LostPublicationClaim(
                    "Video publication claim is no longer current"
                )
