from datetime import UTC, datetime, timedelta

import test_store

from postpilot.engagement.store import EngagementStore
from postpilot.links.store import LinkStore, ShortLink
from postpilot.sources.github import RepoTraffic
from postpilot.stats import Stats, StatsStore, TrafficStats
from postpilot.store.repo import ItemRepo

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn


def test_stats_store_aggregates_windows_and_latest_snapshots(conn):
    now = datetime(2026, 11, 20, 12, tzinfo=UTC)
    since = now - timedelta(days=30)
    too_old = since - timedelta(microseconds=1)
    future = now + timedelta(microseconds=1)
    store = StatsStore(conn)
    assert store.read(now) == Stats()

    engagement = EngagementStore(conn)
    for index, (channel, kind, occurred_at) in enumerate(
        [
            ("linkedin", "comment", since),
            ("linkedin", "comment", now),
            ("linkedin", "reaction", now),
            ("threads", "comment", now),
            ("threads", "comment", too_old),
            ("threads", "reaction", future),
        ]
    ):
        engagement.insert_if_new(
            channel, kind, str(index), "Actor", "Comment", None, "Draft", occurred_at
        )

    for status, updated_at in (
        ("PUBLISHED", since),
        ("PUBLISHED", now),
        ("PUBLISHED", too_old),
        ("PUBLISHED", future),
        ("FAILED", now),
        ("PUBLISHING", now),
    ):
        item = ItemRepo(conn).create_item("github")
        with conn.transaction():
            conn.execute(
                "INSERT INTO publications (item_id, channel, status, updated_at)"
                " VALUES (%s, 'threads', %s, %s)",
                (item.id, status, updated_at),
            )
            conn.execute(
                "WITH render AS ("
                " INSERT INTO video_renders (item_id, lang, status)"
                " VALUES (%s, 'de', 'APPROVED') RETURNING id)"
                " INSERT INTO video_publications (render_id, channel, status, updated_at)"
                " SELECT id, 'custom', %s, %s FROM render",
                (item.id, status, updated_at),
            )

    links = LinkStore(conn)
    day, baseline = now.date(), since.date()
    prior = baseline - timedelta(days=1)
    for link_id, created_on, snapshots in (
        ("a", prior, [(prior, 5), (baseline, 10), (day, 16)]),
        ("b", day, [(day, 9)]),
        ("c", prior, [(prior, 30), (day, 6)]),
        ("d", prior, [(prior, 2)]),
        ("e", day, []),
    ):
        links.save_link(ShortLink(link_id, None, link_id, "de", "", "", created_on))
        for snapshot_day, clicks in snapshots:
            links.upsert_clicks(link_id, snapshot_day, clicks)

    links.upsert_traffic("old-only", prior, RepoTraffic(100, 90, 80, 70, {}))
    for repo, views in (("first", 8), ("second", 6), ("third", 4), ("fourth", 2)):
        links.upsert_traffic(repo, day, RepoTraffic(views, views - 1, 1, 1, {}))

    assert store.read(now) == Stats(
        engagement=(
            ("linkedin", "comment", 2),
            ("linkedin", "reaction", 1),
            ("threads", "comment", 1),
        ),
        published=(("custom (reels)", 2), ("threads", 2)),
        total_clicks=33,
        recent_clicks=15,
        top_links=(("a", "de", 16), ("b", "de", 9), ("c", "de", 6)),
        traffic=TrafficStats(
            day, 20, 16, 4, (("first", 8), ("second", 6), ("third", 4))
        ),
    )
