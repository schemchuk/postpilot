import pytest
import test_store
from psycopg.errors import UniqueViolation

from postpilot.sources.series import ActiveSeriesExists, SeriesStore

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
