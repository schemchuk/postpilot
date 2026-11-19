from dataclasses import dataclass
from datetime import date
from uuid import UUID

from psycopg import Connection
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from postpilot.sources.github import RepoTraffic

_COLUMNS = "link_id, item_id, repo, lang, short_url, destination, created_on"


@dataclass(frozen=True)
class ShortLink:
    link_id: str
    item_id: UUID | None
    repo: str
    lang: str
    short_url: str
    destination: str
    created_on: date


class LinkStore:
    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn

    def save_link(self, link: ShortLink) -> None:
        with self.conn.transaction():
            self.conn.execute(
                f"""
                INSERT INTO short_links ({_COLUMNS})
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (link_id) DO NOTHING
                """,
                (
                    link.link_id,
                    link.item_id,
                    link.repo,
                    link.lang,
                    link.short_url,
                    link.destination,
                    link.created_on,
                ),
            )

    def list_links(self) -> list[ShortLink]:
        with self.conn.transaction():
            rows = self.conn.execute(
                f"SELECT {_COLUMNS} FROM short_links ORDER BY link_id"
            ).fetchall()
        return [ShortLink(*row) for row in rows]

    def upsert_clicks(self, link_id: str, day: date, clicks: int) -> None:
        with self.conn.transaction():
            self.conn.execute(
                """
                INSERT INTO link_clicks (link_id, day, clicks) VALUES (%s, %s, %s)
                ON CONFLICT (link_id, day) DO UPDATE SET clicks = EXCLUDED.clicks
                """,
                (link_id, day, clicks),
            )

    def upsert_traffic(self, repo: str, day: date, traffic: RepoTraffic) -> None:
        with self.conn.transaction():
            self.conn.execute(
                """
                INSERT INTO traffic_snapshots
                    (repo, day, views, unique_views, clones, unique_clones, referrers)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (repo, day) DO UPDATE SET
                    views = EXCLUDED.views, unique_views = EXCLUDED.unique_views,
                    clones = EXCLUDED.clones, unique_clones = EXCLUDED.unique_clones,
                    referrers = EXCLUDED.referrers
                """,
                (
                    repo,
                    day,
                    traffic.views,
                    traffic.unique_views,
                    traffic.clones,
                    traffic.unique_clones,
                    Jsonb(traffic.referrers),
                ),
            )

    def summary(self) -> list[tuple[ShortLink, int]]:
        with self.conn.transaction():
            rows = self.conn.execute(
                f"""
                SELECT {_COLUMNS}, COALESCE(latest.clicks, 0) AS clicks
                FROM short_links
                LEFT JOIN LATERAL (
                    SELECT clicks FROM link_clicks AS snapshot
                    WHERE snapshot.link_id = short_links.link_id
                    ORDER BY day DESC LIMIT 1
                ) AS latest ON true
                ORDER BY clicks DESC, link_id
                """
            ).fetchall()
        return [(ShortLink(*fields), clicks) for *fields, clicks in rows]
