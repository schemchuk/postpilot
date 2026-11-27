from uuid import uuid4

import pytest
import test_store
from psycopg.errors import CheckViolation, RaiseException

from postpilot.projects import FACTORY_PROJECT, Projects
from postpilot.store import migrate
from postpilot.store.project_items import ProjectItem, ProjectItemStore

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn


def test_project_items_migration_round_trip(conn):
    # Step back to the schema before 0021.
    migrate.down(conn, 5)
    with conn.transaction():
        conn.execute(
            "INSERT INTO items (id, source, state) VALUES (%s, 'factory', 'APPROVED')",
            (original_id := uuid4(),),
        )
        original = conn.execute(
            """
            INSERT INTO factory_reels
                (episode, tag, item_id, ai, release_at, post_de, post_threads,
                 threads_skipped, assets, duration, title_de, created_at, updated_at)
            VALUES ('original', 'reel/original', %s, true, now(), 'Post', 'Thread',
                    false, '{"video": {"sha256": "abc", "size": 123}}', 30, 'Title',
                    '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z')
            RETURNING episode, tag, item_id, ai, release_at, post_de, post_threads,
                      threads_skipped, assets, duration, title_de, created_at, updated_at
            """,
            (original_id,),
        ).fetchone()
    migrate.up(conn)
    with conn.transaction():
        assert conn.execute(
            """
            SELECT id, tag, item_id, ai, release_at, post_de, post_threads,
                   threads_skipped, assets, duration, title_de, created_at, updated_at,
                   project, kind, approval
            FROM project_items
            """
        ).fetchone() == (*original, "beekeeper", "reel", "auto")
        assert conn.execute("SELECT source FROM items").fetchall() == [("project",)]
    with pytest.raises(CheckViolation), conn.transaction():
        conn.execute("UPDATE items SET source = 'factory'")

    store = ProjectItemStore(conn)
    projects = Projects.load()
    assert (
        store.create_approved_reel(
            projects,
            ProjectItem(
                "new", original[4], False, "New", None, {}, 10, FACTORY_PROJECT
            ),
        )
        == "new"
    )
    with conn.transaction():
        conn.execute(
            "UPDATE project_items SET title_de = 'Changed' WHERE id = 'original'"
        )

    migrate.down(conn, 5)
    with conn.transaction():
        assert conn.execute(
            """
            SELECT episode, tag, item_id, ai, release_at, post_de, post_threads,
                   threads_skipped, assets, duration, title_de, created_at, updated_at
            FROM factory_reels WHERE episode = 'original'
            """
        ).fetchone() == (*original[:10], "Changed", *original[11:])
        assert conn.execute(
            """
            SELECT i.source, f.episode FROM factory_reels f JOIN items i ON i.id = f.item_id
            ORDER BY f.episode
            """
        ).fetchall() == [("factory", "new"), ("factory", "original")]
    with pytest.raises(CheckViolation), conn.transaction():
        conn.execute("UPDATE items SET source = 'project'")
    migrate.up(conn)


@pytest.mark.parametrize(
    ("project", "kind"), [("other", "reel"), ("beekeeper", "post")]
)
def test_down_refuses_unrepresentable_rows(conn, project, kind):
    with conn.transaction():
        conn.execute(
            "INSERT INTO items (id, source, state) VALUES (%s, 'project', 'APPROVED')",
            (item_id := uuid4(),),
        )
        conn.execute(
            """
            INSERT INTO project_items
                (project, kind, id, tag, item_id, approval, ai, release_at,
                 post_de, threads_skipped, assets, duration)
            VALUES (%s, %s, 'extra', %s, %s, 'auto', false, now(), 'Post', true, '{}', 30)
            """,
            (project, kind, f"{kind}/extra", item_id),
        )
    try:
        migrate.down(conn, 4)  # 0025 through 0022
        with pytest.raises(RaiseException, match="rows exist outside beekeeper reels"):
            migrate.down(conn)
        with conn.transaction():
            assert conn.execute(
                """
                SELECT p.project, p.kind, i.source
                FROM project_items p JOIN items i ON i.id = p.item_id WHERE i.id = %s
                """,
                (item_id,),
            ).fetchone() == (project, kind, "project")
    finally:
        with conn.transaction():
            conn.execute("DELETE FROM project_items WHERE item_id = %s", (item_id,))
            conn.execute("DELETE FROM items WHERE id = %s", (item_id,))
        migrate.up(conn)


def test_reel_ids_are_scoped_by_project_and_due_across_projects(conn):
    from dataclasses import replace
    from datetime import UTC, datetime

    projects = Projects.load()
    beekeeper = projects.get(FACTORY_PROJECT)
    other = replace(beekeeper, key="other", repo="owner/other")
    registry = Projects((beekeeper, other))
    store = ProjectItemStore(conn)
    now = datetime.now(UTC)
    reel = ProjectItem(
        uuid4().hex, now, False, "Original", None, {}, 10, FACTORY_PROJECT
    )
    other_reel = replace(reel, project=other.key, post_de="Other")
    assert store.create_approved_reel(registry, reel) == "new"
    assert store.create_approved_reel(registry, other_reel) == "new"
    assert store.create_approved_reel(registry, other_reel) == "unchanged"
    due = (store.get_by_item(item_id) for item_id in store.list_due_item_ids(now))
    assert {row.project: row.post_de for row in due if row.id == reel.id} == {
        FACTORY_PROJECT: "Original",
        other.key: "Other",
    }
