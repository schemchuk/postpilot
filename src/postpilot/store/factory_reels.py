from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypedDict
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from postpilot.store.repo import ItemRepo

type IntakeStatus = Literal["new", "replaced", "unchanged", "refused"]


class Asset(TypedDict):
    sha256: str
    size: int


@dataclass(frozen=True)
class FactoryReel:
    episode: str
    release_at: datetime
    ai: bool
    post_de: str
    post_threads: str | None
    assets: dict[str, Asset]
    duration: float

    item_id: UUID | None = None
    threads_skipped: bool = False
    title_de: str | None = None

    @property
    def tag(self) -> str:
        return f"reel/{self.episode}"


class FactoryReelStore:
    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn

    def get_by_item(self, item_id: UUID) -> FactoryReel | None:
        with self.conn.transaction():
            row = self.conn.execute(
                """
                SELECT episode, release_at, ai, post_de, post_threads, assets,
                    duration, item_id, threads_skipped, title_de
                FROM factory_reels WHERE item_id = %s
                """,
                (item_id,),
            ).fetchone()
        return FactoryReel(*row) if row else None

    def list_due_item_ids(self, now: datetime) -> list[UUID]:
        with self.conn.transaction():
            rows = self.conn.execute(
                """
                SELECT f.item_id FROM factory_reels f JOIN items i ON i.id = f.item_id
                WHERE i.source = 'factory' AND i.state IN ('APPROVED', 'PUBLISHING')
                    AND f.release_at <= %s
                ORDER BY f.release_at, f.item_id
                """,
                (now,),
            ).fetchall()
        return [item_id for (item_id,) in rows]

    @contextmanager
    def lock_item(self, item_id: UUID) -> Iterator[None]:
        with self.conn.transaction():
            self.conn.execute(
                "SELECT id FROM items WHERE id = %s FOR UPDATE", (item_id,)
            )
            yield

    def create_factory_item(self, reel: FactoryReel) -> IntakeStatus:
        with self.conn.transaction():
            # Serialize concurrent first inserts before there is an episode row to lock.
            self.conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext(%s))",
                (f"factory:{reel.episode}",),
            )
            previous = self.conn.execute(
                """
                SELECT f.item_id, f.release_at, f.ai, f.assets, f.title_de FROM factory_reels f
                JOIN items i ON i.id = f.item_id
                WHERE f.episode = %s FOR UPDATE OF f, i
                """,
                (reel.episode,),
            ).fetchone()
            values = (
                reel.release_at,
                reel.ai,
                Jsonb(reel.assets),
                reel.post_de,
                reel.post_threads,
                reel.post_threads is None,
                reel.duration,
                reel.title_de,
            )
            repo = ItemRepo(self.conn)
            if previous is None:
                item_id = uuid4()
                self.conn.execute(
                    "INSERT INTO items (id, source, state, approved_at) "
                    "VALUES (%s, 'factory', 'APPROVED', now())",
                    (item_id,),
                )
                self.conn.execute(
                    """
                    INSERT INTO factory_reels (episode, tag, item_id, release_at, ai,
                        assets, post_de, post_threads, threads_skipped, duration, title_de)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (reel.episode, reel.tag, item_id, *values),
                )
                repo.add_variant(item_id, "de", reel.post_de)
                return "new"

            item_id, release_at, ai, assets, title_de = previous
            if (release_at, ai, assets, title_de) == (
                reel.release_at,
                reel.ai,
                reel.assets,
                reel.title_de,
            ):
                return "unchanged"
            if self.conn.execute(
                "SELECT 1 FROM item_publications WHERE item_id = %s "
                "AND (status IN ('PUBLISHED', 'PUBLISHING') "
                "OR (channel = 'youtube' AND remote_ref IS NOT NULL)) LIMIT 1",
                (item_id,),
            ).fetchone():
                return "refused"
            self.conn.execute(
                """
                UPDATE factory_reels SET release_at = %s, ai = %s, assets = %s,
                    post_de = %s, post_threads = %s, threads_skipped = %s, duration = %s, title_de = %s,
                    updated_at = now()
                WHERE episode = %s
                """,
                (*values, reel.episode),
            )
            self.conn.execute(
                "UPDATE variants SET text = %s WHERE item_id = %s AND lang = 'de'",
                (reel.post_de, item_id),
            )
            return "replaced"
