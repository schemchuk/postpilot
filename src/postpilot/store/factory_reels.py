from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypedDict
from uuid import uuid4

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

    @property
    def tag(self) -> str:
        return f"reel/{self.episode}"


class FactoryReelStore:
    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn

    def create_factory_item(self, reel: FactoryReel) -> IntakeStatus:
        with self.conn.transaction():
            # Serialize concurrent first inserts before there is an episode row to lock.
            self.conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext(%s))",
                (f"factory:{reel.episode}",),
            )
            previous = self.conn.execute(
                """
                SELECT f.item_id, f.release_at, f.ai, f.assets FROM factory_reels f
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
                        assets, post_de, post_threads, threads_skipped, duration)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (reel.episode, reel.tag, item_id, *values),
                )
                repo.add_variant(item_id, "de", reel.post_de)
                return "new"

            item_id, release_at, ai, assets = previous
            if (release_at, ai, assets) == (reel.release_at, reel.ai, reel.assets):
                return "unchanged"
            if self.conn.execute(
                "SELECT 1 FROM publications WHERE item_id = %s "
                "AND status IN ('PUBLISHED', 'PUBLISHING') LIMIT 1",
                (item_id,),
            ).fetchone():
                return "refused"
            self.conn.execute(
                """
                UPDATE factory_reels SET release_at = %s, ai = %s, assets = %s,
                    post_de = %s, post_threads = %s, threads_skipped = %s, duration = %s,
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
