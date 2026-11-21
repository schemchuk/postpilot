from datetime import date

import pytest
import test_store
from psycopg.errors import UniqueViolation

from postpilot.sources.series import (
    ActiveSeriesExists,
    NoActiveSeries,
    QueuedSeriesExists,
    SeriesStore,
)

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn


def test_series_lifecycle(conn):
    store = SeriesStore(conn)
    assert store.get_active() is None
    series = store.start("Project", 2)
    assert series.part == 1
    assert store.get_active() == series
    with pytest.raises(ActiveSeriesExists):
        store.start("other", None)
    with pytest.raises(UniqueViolation), conn.transaction():
        conn.execute(
            "INSERT INTO series (id, repo) VALUES (gen_random_uuid(), 'other')"
        )
    assert store.record_part("other", 1, "stale", "stale") is None
    updated = store.record_part("project", 1, "Eins", "Один")
    assert updated is not None
    assert updated.part == 2
    assert store.get_active() == updated
    assert store.record_part("Project", 1, "stale", "stale") is None
    completed = store.record_part("Project", 2, "Zwei", "Два")
    assert completed is not None
    assert completed.status == "completed"
    assert store.get_active() is None
    assert store.record_part("Project", 2, "retry", "retry") is None
    assert [(p.part, p.text_de, p.text_ua) for p in store.history(series)] == [
        (1, "Eins", "Один"),
        (2, "Zwei", "Два"),
    ]
    assert "project" in store.posted_repos()


def test_stop_and_restart_unlimited_series(conn):
    store = SeriesStore(conn)
    old = store.start("unlimited", None)
    assert store.stop().status == "stopped"
    assert store.stop() is None
    assert store.record_part("unlimited", 1, "stale", "stale") is None
    new = store.start("unlimited", None)
    assert store.record_part("unlimited", 1, "stale", "stale", series_id=old.id) is None
    assert store.history(old) == []
    updated = store.record_part("unlimited", 1, "Eins", "Один", series_id=new.id)
    assert updated is not None
    assert updated.part == 2
    assert updated.status == "active"
    assert store.stop() is not None


def test_mark_posted_is_idempotent(conn):
    store = SeriesStore(conn)
    before = store.posted_repos()
    store.mark_posted("Weekly")
    store.mark_posted("weekly")
    assert store.posted_repos() == before | {"weekly"}


def test_import_series(conn):
    store = SeriesStore(conn)
    started = date(2026, 11, 15)
    store.mark_posted("Project")
    series = store.import_series(
        repo="Project",
        total_parts=5,
        part=3,
        started_on=started,
        published=[(1, "Eins", "Один", started)],
        planned=[(3, "Drei", "Три"), (5, "Fünf", "П'ять")],
        posted_repos=["PROJECT", "Other"],
    )
    assert store.get_active() == series
    assert (series.repo, series.part, series.total_parts, series.started_on) == (
        "Project",
        3,
        5,
        started,
    )
    assert [
        (p.part, p.text_de, p.text_ua, p.published_on) for p in store.history(series)
    ] == [(1, "Eins", "Один", started)]
    assert store.planned_part(series) == ("Drei", "Три")
    assert store.posted_repos() >= {"project", "other"}
    fourth = store.record_part("Project", 3, "Drei", "Три", series_id=series.id)
    assert fourth is not None and store.planned_part(fourth) is None
    fifth = store.record_part("Project", 4, "Vier", "Чотири", series_id=series.id)
    assert fifth is not None and store.planned_part(fifth) == ("Fünf", "П'ять")
    store.stop()


@pytest.mark.parametrize("already_active", [True, False])
def test_failed_series_import_leaves_tables_unchanged(conn, already_active):
    store = SeriesStore(conn)
    active = store.start("Existing", 4) if already_active else None
    tables = ("series", "series_parts", "series_planned_parts", "posted_repos")

    def counts():
        return [
            conn.execute(f"SELECT count(*) FROM {table}").fetchone() for table in tables
        ]

    with conn.transaction():
        before = counts()
        with pytest.raises(ActiveSeriesExists if already_active else UniqueViolation):
            store.import_series(
                repo="Project",
                total_parts=5,
                part=3,
                started_on=date(2026, 11, 15),
                published=[(1, "Eins", "Один", date(2026, 11, 15))],
                planned=[(3, "Drei", "Три"), (3, "Duplicate", "Duplicate")],
                posted_repos=["Should not persist"],
            )
        assert counts() == before
        assert store.get_active() == active
    if already_active:
        store.stop()


def test_queue_requires_active_and_allows_one(conn):
    store = SeriesStore(conn)
    with pytest.raises(NoActiveSeries):
        store.queue("next", 5)
    store.start("current", 2)
    queued = store.queue("next", 5)
    assert (queued.status, queued.part, queued.total_parts) == ("queued", 1, 5)
    assert store.get_queued() == queued
    with pytest.raises(QueuedSeriesExists):
        store.queue("other", None)
    assert store.cancel_queued() == queued
    assert store.cancel_queued() is None
    assert store.get_queued() is None
    store.stop()


def test_last_part_promotes_queued_series(conn):
    store = SeriesStore(conn)
    current = store.start("current", 2)
    store.queue("next", 5)
    store.record_part("current", 1, "Eins", "Один", series_id=current.id)
    assert store.get_queued() is not None
    store.record_part("current", 2, "Zwei", "Два", series_id=current.id)
    active = store.get_active()
    assert active is not None
    assert (active.repo, active.part, active.status) == ("next", 1, "active")
    assert active.started_on == conn.execute("SELECT CURRENT_DATE").fetchone()[0]
    assert store.get_queued() is None
    store.stop()


@pytest.mark.parametrize("end", ["complete", "stop"])
def test_complete_and_stop_promote_queued_series(conn, end):
    store = SeriesStore(conn)
    current = store.start("current", None)
    store.queue("next", 3)
    if end == "complete":
        assert store.complete(series_id=current.id) is not None
    else:
        assert store.stop() is not None
    active = store.get_active()
    assert active is not None and active.repo == "next"
    assert store.get_queued() is None
    assert store.stop() is not None
    assert store.get_active() is None
