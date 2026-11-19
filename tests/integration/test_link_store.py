from datetime import date, timedelta

import test_store

from postpilot.links.store import LinkStore, ShortLink
from postpilot.sources.github import RepoTraffic
from postpilot.store.repo import ItemRepo

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn


def test_links_and_snapshots_persist_without_duplicate_days(conn, database_schema):
    store = LinkStore(conn)
    day = date(2026, 11, 19)
    yesterday = day - timedelta(days=1)
    item_id = ItemRepo(conn).create_item("github").id
    first = ShortLink(
        "a", item_id, "repo", "de", "https://s.test/a", "https://d.test", day
    )
    second = ShortLink(
        "b", None, "repo", "ua", "https://s.test/b", "https://d.test", day
    )
    third = ShortLink(
        "c", None, "repo", "de", "https://s.test/c", "https://d.test", day
    )
    for link in (first, second, third, first):
        store.save_link(link)
    store.upsert_clicks("a", day, 2)
    store.upsert_clicks("a", yesterday, 20)
    store.upsert_clicks("a", day, 4)
    store.upsert_clicks("b", day, 5)
    old = RepoTraffic(1, 1, 0, 0, {})
    updated = RepoTraffic(7, 6, 5, 4, {"github.com": 3})
    store.upsert_traffic("repo", day, old)
    store.upsert_traffic("repo", yesterday, old)
    store.upsert_traffic("repo", day, updated)

    with test_store.connect(test_store.DATABASE_URL, schema=database_schema) as fresh:
        persisted = LinkStore(fresh)
        assert persisted.list_links() == [first, second, third]
        assert persisted.summary() == [(second, 5), (first, 4), (third, 0)]
        assert fresh.execute(
            "SELECT link_id, day, clicks FROM link_clicks ORDER BY link_id, day"
        ).fetchall() == [("a", yesterday, 20), ("a", day, 4), ("b", day, 5)]
        assert fresh.execute(
            "SELECT * FROM traffic_snapshots ORDER BY day"
        ).fetchall() == [
            ("repo", yesterday, 1, 1, 0, 0, {}),
            ("repo", day, 7, 6, 5, 4, {"github.com": 3}),
        ]
