import os
from uuid import uuid4

import pytest
from psycopg import sql
from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation

from postpilot.core.models import PublicationStatus, Variant
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.store import migrate
from postpilot.store.db import connect
from postpilot.store.repo import ItemRepo

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is unset"),
]


@pytest.fixture(scope="module")
def database_schema():
    name = f"test_{uuid4().hex}"
    with connect(DATABASE_URL) as admin:
        with admin.transaction():
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            with connect(DATABASE_URL, schema=name) as conn:
                migrate.up(conn)
            yield name
        finally:
            with admin.transaction():
                admin.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name))
                )


@pytest.fixture
def conn(database_schema):
    with connect(DATABASE_URL, schema=database_schema) as connection:
        yield connection


@pytest.fixture
def repo(conn):
    return ItemRepo(conn)


@pytest.fixture
def approved_item(repo):
    item = repo.create_item("github")
    repo.transition(item.id, ItemState.AWAITING_APPROVAL)
    return repo.transition(item.id, ItemState.APPROVED, approved_by=12345678901)


def table_names(conn):
    with conn.transaction():
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        ).fetchall()
    return {name for (name,) in rows}


def applied_migrations(conn):
    with conn.transaction():
        return conn.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()


def test_migration_up_noop_and_down(conn):
    tables = {"items", "variants", "publications", "schema_migrations"}
    bot_tables = {"bot_state", "pending_edits"}
    series_tables = {"series", "series_parts", "posted_repos"}
    full_schema = tables | bot_tables | {"tokens", "inbox_updates"} | series_tables
    link_tables = {"short_links", "link_clicks", "traffic_snapshots"}
    full_schema |= link_tables
    assert table_names(conn) == full_schema
    initial = applied_migrations(conn)
    assert [version for version, _ in initial] == [
        "0001_init",
        "0002_bot_state",
        "0003_tokens",
        "0004_series",
        "0005_inbox_updates",
        "0006_link_tracking",
    ]
    migrate.up(conn)
    assert applied_migrations(conn) == initial

    migrate.down(conn)
    assert table_names(conn) == full_schema - link_tables
    assert applied_migrations(conn) == initial[:5]

    migrate.down(conn)
    assert table_names(conn) == full_schema - link_tables - {"inbox_updates"}
    assert applied_migrations(conn) == initial[:4]

    migrate.down(conn)
    assert table_names(conn) == tables | bot_tables | {"tokens"}
    assert applied_migrations(conn) == initial[:3]

    migrate.down(conn)
    assert table_names(conn) == tables | bot_tables
    assert applied_migrations(conn) == initial[:2]

    migrate.down(conn)
    assert table_names(conn) == tables
    assert applied_migrations(conn) == initial[:1]

    migrate.down(conn)
    assert table_names(conn) == {"schema_migrations"}
    assert applied_migrations(conn) == []

    migrate.up(conn)
    assert table_names(conn) == full_schema


def test_illegal_transition_preserves_item(repo):
    item = repo.create_item("telegram")
    with pytest.raises(IllegalTransition):
        repo.transition(item.id, ItemState.PUBLISHING)
    assert repo.get_item(item.id) == item


def test_approval_records_approver_and_time(repo):
    item = repo.create_item("github")
    awaiting = repo.transition(item.id, ItemState.AWAITING_APPROVAL)
    with pytest.raises(ValueError, match="approved_by"):
        repo.transition(item.id, ItemState.APPROVED)
    assert repo.get_item(item.id) == awaiting
    approved = repo.transition(item.id, ItemState.APPROVED, approved_by=12345678901)
    assert approved.approved_by == 12345678901
    assert approved.approved_at == approved.updated_at
    assert approved.approved_at >= awaiting.updated_at
    assert repo.get_item(item.id) == approved


def test_draft_cannot_claim_publication(repo):
    item = repo.create_item("telegram")
    with pytest.raises(IllegalTransition):
        repo.claim_publication(item.id, "example")


def test_claim_only_once_per_channel(conn, repo, approved_item):
    claimed = repo.claim_publication(approved_item.id, "example")
    assert claimed is not None
    assert not claimed.recovered
    assert claimed.publication.status is PublicationStatus.PUBLISHING
    assert repo.claim_publication(approved_item.id, "example") is None
    repo.mark_published(approved_item.id, "example", "remote-id")
    assert repo.claim_publication(approved_item.id, "example") is None
    assert repo.claim_publication(approved_item.id, "another") is not None
    with conn.transaction():
        row = conn.execute(
            "SELECT status, remote_id, error FROM publications WHERE item_id = %s AND channel = %s",
            (approved_item.id, "example"),
        ).fetchone()
    assert row == ("PUBLISHED", "remote-id", None)


def test_failed_publication_can_be_reclaimed(conn, repo, approved_item):
    item_id = approved_item.id
    repo.transition(item_id, ItemState.PUBLISHING)
    assert repo.claim_publication(item_id, "example") is not None
    repo.mark_failed(item_id, "example", "Temporary failure")
    with conn.transaction():
        row = conn.execute(
            "SELECT status, error FROM publications WHERE item_id = %s AND channel = %s",
            (item_id, "example"),
        ).fetchone()
    assert row == ("FAILED", "Temporary failure")

    repo.transition(item_id, ItemState.FAILED)
    with pytest.raises(IllegalTransition):
        repo.claim_publication(item_id, "example")
    repo.transition(item_id, ItemState.PUBLISHING)
    claimed = repo.claim_publication(item_id, "example")
    assert claimed is not None
    assert claimed.recovered
    assert not claimed.stale
    assert claimed.publication.status is PublicationStatus.PUBLISHING
    assert claimed.publication.error is None
    assert repo.claim_publication(item_id, "example") is None


def test_variant_foreign_key_and_unique_constraints(repo):
    item = repo.create_item("telegram")
    with pytest.raises(ForeignKeyViolation):
        repo.add_variant(uuid4(), "ua", "Text")
    repo.add_variant(item.id, "ua", "Original")
    with pytest.raises(UniqueViolation):
        repo.add_variant(item.id, "ua", "Duplicate")
    assert repo.get_variants(item.id) == [Variant(item.id, "ua", "Original")]


def test_publication_foreign_key_and_unique_constraints(conn, repo, approved_item):
    statement = "INSERT INTO publications (item_id, channel, status) VALUES (%s, %s, 'PUBLISHING')"
    with pytest.raises(ForeignKeyViolation), conn.transaction():
        conn.execute(statement, (uuid4(), "example"))
    assert repo.claim_publication(approved_item.id, "example") is not None
    with pytest.raises(UniqueViolation), conn.transaction():
        conn.execute(statement, (approved_item.id, "example"))


def test_database_checks_state_and_status(conn, approved_item):
    with pytest.raises(CheckViolation), conn.transaction():
        conn.execute(
            "UPDATE items SET state = 'INVALID' WHERE id = %s", (approved_item.id,)
        )
    with pytest.raises(CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO publications (item_id, channel, status) VALUES (%s, %s, 'DRAFT')",
            (approved_item.id, "example"),
        )
