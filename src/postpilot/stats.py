from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html import escape

from psycopg import Connection
from psycopg.rows import TupleRow

_WINDOW = timedelta(days=30)


@dataclass(frozen=True)
class TrafficStats:
    day: date
    views: int
    unique_views: int
    clones: int
    top_repos: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class Stats:
    engagement: tuple[tuple[str, str, int], ...] = ()
    published: tuple[tuple[str, int], ...] = ()
    total_clicks: int = 0
    recent_clicks: int = 0
    top_links: tuple[tuple[str, str, int], ...] = ()
    traffic: TrafficStats | None = None


class StatsStore:
    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn

    def read(self, now: datetime) -> Stats:
        now = now.astimezone(UTC)
        since = now - _WINDOW
        with self.conn.transaction():
            engagement = self.conn.execute(
                """
                SELECT channel, kind, count(*)
                FROM engagement_events WHERE occurred_at BETWEEN %s AND %s
                GROUP BY channel, kind ORDER BY channel, kind
                """,
                (since, now),
            ).fetchall()
            published = self.conn.execute(
                """
                SELECT channel, count(*) FROM publications
                WHERE status = 'PUBLISHED' AND updated_at BETWEEN %s AND %s
                GROUP BY channel
                UNION ALL
                SELECT channel || ' (reels)', count(*) FROM video_publications
                WHERE status = 'PUBLISHED' AND updated_at BETWEEN %s AND %s
                GROUP BY channel
                ORDER BY channel
                """,
                (since, now, since, now),
            ).fetchall()
            clicks = self.conn.execute(
                """
                SELECT repo, lang, COALESCE(latest.clicks, 0) AS total,
                       GREATEST(COALESCE(latest.clicks, 0)::bigint
                                - COALESCE(baseline.clicks, 0), 0) AS recent
                FROM short_links
                LEFT JOIN LATERAL (
                    SELECT clicks FROM link_clicks
                    WHERE link_id = short_links.link_id
                    ORDER BY day DESC LIMIT 1
                ) AS latest ON true
                LEFT JOIN LATERAL (
                    SELECT clicks FROM link_clicks
                    WHERE link_id = short_links.link_id AND day <= %s
                    ORDER BY day DESC LIMIT 1
                ) AS baseline ON true
                ORDER BY total DESC, short_links.link_id
                """,
                (since.date(),),
            ).fetchall()
            traffic = self.conn.execute(
                """
                SELECT day, repo, views, unique_views, clones FROM traffic_snapshots
                WHERE day = (SELECT max(day) FROM traffic_snapshots)
                ORDER BY views DESC, repo
                """
            ).fetchall()

        return Stats(
            engagement=tuple(engagement),
            published=tuple(published),
            total_clicks=sum(total for _, _, total, _ in clicks),
            recent_clicks=sum(recent for _, _, _, recent in clicks),
            top_links=tuple((repo, lang, total) for repo, lang, total, _ in clicks[:3]),
            traffic=TrafficStats(
                day=traffic[0][0],
                views=sum(views for _, _, views, _, _ in traffic),
                unique_views=sum(unique for _, _, _, unique, _ in traffic),
                clones=sum(clones for _, _, _, _, clones in traffic),
                top_repos=tuple((repo, views) for _, repo, views, _, _ in traffic[:3]),
            )
            if traffic
            else None,
        )


def _label(value: str) -> str:
    parts = []
    remaining = 140
    for char in value:
        escaped = escape(char)
        size = len(escaped) + (ord(char) > 0xFFFF)
        if size > remaining:
            parts.append("…")
            break
        parts.append(escaped)
        remaining -= size
    return "".join(parts)


def _section(title: str, lines: Iterable[str]) -> str:
    parts = [f"<b>{title}</b>"]
    remaining = 1000 - len(parts[0]) - 2  # Reserve space for a truncation marker.
    for line in lines:
        size = len(line.encode("utf-16-le")) // 2 + 1
        if size > remaining:
            parts.append("…")
            break
        parts.append(line)
        remaining -= size
    if len(parts) == 1:
        parts.append("None.")
    return "\n".join(parts)


def format_stats(stats: Stats) -> str:
    engagement = (
        f"{_label(channel)} · {_label(kind)}: {count}"
        for channel, kind, count in stats.engagement
    )
    published = (f"{_label(channel)}: {count}" for channel, count in stats.published)
    clicks = [
        f"Total: {stats.total_clicks} · Last 30 days: {stats.recent_clicks}",
        *(
            f"{_label(repo)} ({_label(lang)}): {count}"
            for repo, lang, count in stats.top_links
        ),
    ]
    traffic = stats.traffic
    traffic_lines = (
        [
            (
                f"{traffic.day.isoformat()} · Views: {traffic.views} · "
                f"Unique views: {traffic.unique_views} · Clones: {traffic.clones}"
            ),
            *(f"{_label(repo)}: {views}" for repo, views in traffic.top_repos),
        ]
        if traffic is not None
        else ["No traffic snapshot."]
    )
    return "\n\n".join(
        (
            _section("Engagement (30 days)", engagement),
            _section("Published posts (30 days)", published),
            _section("Short-link clicks", clicks),
            _section("GitHub traffic", traffic_lines),
        )
    )
