import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg import sql
from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation

from postpilot.core.models import PublicationStatus, Variant
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.engagement.store import EngagementStore
from postpilot.store import migrate
from postpilot.store.db import connect
from postpilot.store.repo import ItemRepo, LostPublicationClaim

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
    video_tables = {"video_renders"}
    full_schema |= link_tables | video_tables | {"video_publications"}
    full_schema.add("engagement_events")
    latest_schema = full_schema | {
        "weekly_drafts",
        "series_planned_parts",
        "own_videos",
        "own_video_updates",
        "telegram_inbox",
        "factory_reels",
        "project_items",
        "project_rejections",
        "tiktok_settings",
    }
    assert table_names(conn) == latest_schema
    initial = applied_migrations(conn)
    assert [version for version, _ in initial] == [
        "0001_init",
        "0002_bot_state",
        "0003_tokens",
        "0004_series",
        "0005_inbox_updates",
        "0006_link_tracking",
        "0007_video_renders",
        "0008_publication_remote_ref",
        "0009_video_publications",
        "0010_engagement_events",
        "0011_github_engagement",
        "0012_engagement_replies",
        "0013_weekly_drafts",
        "0014_series_planned_parts",
        "0015_series_queue",
        "0016_own_videos",
        "0017_video_linkedin",
        "0018_telegram_inbox",
        "0019_factory_reels",
        "0020_item_video_publications",
        "0021_project_items",
        "0022_project_intake",
        "0023_tiktok_settings",
        "0024_tiktok_partial_choice",
    ]
    migrate.up(conn)
    assert applied_migrations(conn) == initial
    with conn.transaction():
        conn.execute(
            "INSERT INTO items (id, source, state) VALUES (%s, 'own_video', 'DRAFT')",
            (id := uuid4(),),
        )
        conn.execute("DELETE FROM items WHERE id = %s", (id,))
    migrate.down(conn)
    assert applied_migrations(conn) == initial[:23]
    migrate.down(conn)
    assert applied_migrations(conn) == initial[:22]
    assert "tiktok_settings" not in table_names(conn)
    migrate.down(conn)
    assert applied_migrations(conn) == initial[:21]
    assert "project_rejections" not in table_names(conn)
    migrate.down(conn)
    assert applied_migrations(conn) == initial[:20]
    assert "project_items" not in table_names(conn)
    migrate.down(conn)
    assert applied_migrations(conn) == initial[:19]
    migrate.down(conn)
    assert applied_migrations(conn) == initial[:18]
    assert "factory_reels" not in table_names(conn)
    migrate.down(conn)
    assert applied_migrations(conn) == initial[:17]
    assert table_names(conn) == latest_schema - {
        "telegram_inbox",
        "factory_reels",
        "project_items",
        "project_rejections",
        "tiktok_settings",
    }
    migrate.down(conn)
    assert applied_migrations(conn) == initial[:16]
    migrate.down(conn)
    assert applied_migrations(conn) == initial[:15]
    assert "own_videos" not in table_names(conn)
    assert "own_video_updates" not in table_names(conn)
    with pytest.raises(CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO items (id, source, state) VALUES (%s, 'own_video', 'DRAFT')",
            (id,),
        )
    migrate.down(conn)
    assert applied_migrations(conn) == initial[:14]
    assert table_names(conn) == full_schema | {"weekly_drafts", "series_planned_parts"}
    migrate.down(conn)
    assert applied_migrations(conn) == initial[:13]
    assert table_names(conn) == full_schema | {"weekly_drafts"}
    migrate.down(conn)
    assert applied_migrations(conn) == initial[:12]
    assert table_names(conn) == full_schema

    events = EngagementStore(conn)
    now = datetime.now(UTC)
    star_args = ("github", "star", "star:123:10", None, None, None, None, now)
    star = events.insert_if_new(*star_args, notified=True)
    comment = events.insert_if_new(
        "threads", "comment", "reply-id", None, None, None, None, now
    )
    with pytest.raises(CheckViolation), conn.transaction():
        conn.execute("UPDATE engagement_events SET reply_status = 'INVALID'")
    assert events.claim_reply(comment.id, now).reply_status == "SENDING"
    migrate.down(conn)
    assert applied_migrations(conn) == initial[:11]
    with conn.transaction():
        columns = conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'engagement_events'"
            " AND column_name LIKE 'reply_%'"
        ).fetchall()
    assert columns == [("reply_draft",)]

    migrate.down(conn)
    assert applied_migrations(conn) == initial[:10]
    assert table_names(conn) == full_schema
    assert events.get(star.id) is None
    assert events.get(comment.id) == comment
    with pytest.raises(CheckViolation):
        events.insert_if_new(*star_args)

    migrate.down(conn)
    full_schema.remove("engagement_events")
    assert applied_migrations(conn) == initial[:9]
    assert table_names(conn) == full_schema

    migrate.down(conn)
    assert applied_migrations(conn) == initial[:8]
    assert table_names(conn) == full_schema - {"video_publications"}
    full_schema -= {"video_publications"}

    migrate.down(conn)
    assert applied_migrations(conn) == initial[:7]
    with conn.transaction():
        columns = conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'publications'"
        ).fetchall()
    assert ("remote_ref",) not in columns
    assert ("remote_ref_claimed_at",) not in columns

    migrate.down(conn)
    assert table_names(conn) == full_schema - video_tables
    assert applied_migrations(conn) == initial[:6]

    migrate.down(conn)
    assert table_names(conn) == full_schema - video_tables - link_tables
    assert applied_migrations(conn) == initial[:5]

    migrate.down(conn)
    assert table_names(conn) == full_schema - video_tables - link_tables - {
        "inbox_updates"
    }
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
    assert table_names(conn) == latest_schema


def test_illegal_transition_preserves_item(repo):
    item = repo.create_item("telegram")
    with pytest.raises(IllegalTransition):
        repo.transition(item.id, ItemState.PUBLISHING)
    assert repo.get_item(item.id) == item


@pytest.mark.parametrize("source", ["github", "telegram", "own_video"])
def test_approval_records_approver_and_time(repo, source):
    item = repo.create_item(source)
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


def test_publication_ref_survives_stale_reclaim_and_is_cleared_after_failure(
    repo, approved_item
):
    item_id = approved_item.id
    first = repo.claim_publication(item_id, "threads")
    assert first is not None
    assert first.remote_ref is None
    repo.save_publication_ref(first, "container-1")
    stored = repo.list_publications(item_id)[0]
    assert stored.remote_ref == "container-1"
    assert (
        stored.updated_at
        == stored.remote_ref_claimed_at
        == first.publication.updated_at
    )

    stale = repo.claim_publication(item_id, "threads", stale_after=timedelta(0))
    assert stale is not None and stale.stale
    assert stale.remote_ref == "container-1"
    assert stale.publication.remote_ref_claimed_at == first.publication.updated_at
    with pytest.raises(LostPublicationClaim):
        repo.save_publication_ref(first, "stale-write")
    repo.save_publication_ref(stale, "container-1")
    assert repo.list_publications(item_id)[0].remote_ref_claimed_at == stored.updated_at

    repo.save_publication_ref(stale, "container-2")
    stored = repo.list_publications(item_id)[0]
    assert stored.remote_ref == "container-2"
    assert stored.remote_ref_claimed_at == stale.publication.updated_at

    repo.mark_failed(item_id, "threads", "Rejected", claim=stale)
    with pytest.raises(LostPublicationClaim):
        repo.save_publication_ref(stale, "late-write")
    retried = repo.claim_publication(item_id, "threads")
    assert retried is not None and not retried.stale
    assert retried.remote_ref is None
    assert retried.publication.remote_ref_claimed_at is None


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


@pytest.mark.parametrize("status", ["PUBLISHING", "PUBLISHED"])
def test_factory_intake_repeats_and_publisher_isolation(conn, repo, status):
    from postpilot.core.publisher import Publisher
    from postpilot.projects import FACTORY_PROJECT, Projects
    from postpilot.store.project_items import ProjectItem, ProjectItemStore
    from postpilot.video.publications import VideoPublicationStore
    from postpilot.video.store import VideoStore

    episode = f"002-{uuid4().hex}"
    store = ProjectItemStore(conn)
    projects = Projects.load()
    release_at = datetime.now(UTC)
    reel = ProjectItem(
        episode,
        release_at,
        True,
        "  Verbatim.\n",
        None,
        {"post_de.txt": {"sha256": "a" * 64, "size": 13}},
        3.0,
        FACTORY_PROJECT,
    )
    with pytest.raises(ValueError, match="project intake"):
        repo.create_item("project")
    assert store.create_approved_reel(projects, reel) == "new"

    def persisted():
        with conn.transaction():
            return conn.execute(
                "SELECT item_id, release_at, ai, assets, post_de, post_threads, "
                "threads_skipped, updated_at FROM project_items WHERE project = %s AND id = %s",
                (FACTORY_PROJECT, episode),
            ).fetchone()

    initial = persisted()
    item_id = initial[0]
    item = repo.get_item(item_id)
    assert item.source == "project" and item.state == ItemState.APPROVED
    assert item.approved_by is None and item.approved_at is not None
    assert store.get_by_item(item_id).approval == "auto"
    assert initial[1:7] == (release_at, True, reel.assets, reel.post_de, None, True)
    assert repo.get_variants(item_id) == [Variant(item_id, "de", reel.post_de)]
    assert item_id not in repo.list_approved_item_ids()
    assert repo.list_approved_item_ids(source="project") == []
    assert (
        repo.next_video_variant() is None
        or repo.next_video_variant().item_id != item_id
    )
    with pytest.raises(IllegalTransition, match="project publisher"):
        Publisher(repo, [object()]).publish_item(item_id)

    render_id = VideoStore(conn).claim(item_id, "de")
    assert render_id is not None
    video_publications = VideoPublicationStore(conn)
    with conn.transaction():
        conn.execute(
            "UPDATE video_renders SET status = 'APPROVED' WHERE id = %s", (render_id,)
        )
    assert (
        video_publications.oldest_eligible({"instagram"}, timedelta(minutes=5))
        != render_id
    )
    assert (
        video_publications.claim(render_id, "instagram", timedelta(minutes=5)) is None
    )

    assert store.create_approved_reel(projects, reel) == "unchanged"
    assert persisted() == initial
    changed = replace(reel, release_at=release_at + timedelta(hours=1))
    assert store.create_approved_reel(projects, changed) == "replaced"
    changed = replace(changed, ai=False)
    assert store.create_approved_reel(projects, changed) == "replaced"
    changed = replace(
        changed,
        post_de="Fixed",
        post_threads="Threads",
        assets={"post_de.txt": {"sha256": "b" * 64, "size": 5}},
    )
    assert store.create_approved_reel(projects, changed) == "replaced"
    replacement = persisted()
    assert replacement[:7] == (
        item_id,
        changed.release_at,
        False,
        changed.assets,
        "Fixed",
        "Threads",
        False,
    )
    assert repo.get_variants(item_id) == [Variant(item_id, "de", "Fixed")]
    with conn.transaction():
        conn.execute(
            "INSERT INTO publications (item_id, channel, status) VALUES (%s, 'threads', %s)",
            (item_id, status),
        )
    assert store.create_approved_reel(projects, changed) == "unchanged"
    assert store.create_approved_reel(projects, reel) == "refused"
    assert persisted() == replacement
    assert repo.get_variants(item_id) == [Variant(item_id, "de", "Fixed")]


def test_project_intake_migration_round_trip_with_data(conn):
    from postpilot.projects import FACTORY_PROJECT, Projects
    from postpilot.store.project_items import ProjectItem, ProjectItemStore

    store = ProjectItemStore(conn)
    projects = Projects.load()
    reel = ProjectItem(
        uuid4().hex,
        datetime.now(UTC),
        False,
        "Legacy text",
        None,
        {},
        10,
        FACTORY_PROJECT,
    )
    assert store.create_approved_reel(projects, reel) == "new"
    with conn.transaction():
        item_id = store.preview_item_id(reel)
    assert store.get_by_item(item_id).fingerprint is None
    assert store.get_by_item(item_id).channels is None
    v2 = replace(
        reel,
        release_at=reel.release_at + timedelta(minutes=1),
        fingerprint="revision",
        channels=("threads",),
    )
    assert store.save_reel(projects, v2) == ("replaced", None)
    assert store.record_rejection(reel.project, reel.tag, "bad", "bad manifest")
    assert not store.record_rejection(reel.project, reel.tag, "bad", "bad manifest")
    migrate.down(conn, 2)
    with conn.transaction():
        row = conn.execute(
            "SELECT to_jsonb(f) FROM project_items f WHERE item_id = %s", (item_id,)
        ).fetchone()[0]
    assert (
        "channels" not in row
        and "fingerprint" not in row
        and "preview_message_id" not in row
    )
    assert row["post_de"] == "Legacy text"
    assert "project_rejections" not in table_names(conn)
    migrate.up(conn)
    migrated = store.get_by_item(item_id)
    assert migrated.post_de == reel.post_de
    assert migrated.release_at == v2.release_at
    assert migrated.channels is None and migrated.fingerprint is None
    assert not store.seen(reel.project, reel.tag, "bad")


def test_project_tap_replacement_approval_and_refusal(conn, repo):
    from postpilot.projects import Project, Projects
    from postpilot.store.project_items import ProjectItem, ProjectItemStore

    store = ProjectItemStore(conn)
    project = Project("review", "owner/review", ("reel",), ("threads",))
    projects = Projects((project,))
    now = datetime.now(UTC)
    reel = ProjectItem(
        uuid4().hex,
        now,
        False,
        "Original",
        "Original",
        {},
        10,
        project.key,
        approval="tap",
        channels=("threads",),
        fingerprint="first",
    )
    with conn.transaction():
        assert store.save_reel(projects, reel) == ("new", None)
        item_id = store.preview_item_id(reel)
        store.set_preview_message(item_id, 1)
    assert repo.get_item(item_id).state == ItemState.AWAITING_APPROVAL
    assert item_id not in store.list_due_item_ids(now)
    assert store.resolve_preview(item_id, 1, 7, True)
    assert repo.get_item(item_id).approved_by == 7
    assert item_id in store.list_due_item_ids(now)
    unchanged = replace(reel, fingerprint="second")
    assert store.save_reel(projects, unchanged) == ("unchanged", None)
    assert store.seen(project.key, reel.tag, "second")
    assert not store.resolve_preview(item_id, 1, 7, False)
    changed = replace(reel, ai=True, fingerprint="third")
    with conn.transaction():
        assert store.save_reel(projects, changed) == ("replaced", None)
        store.set_preview_message(item_id, 2)
    item = repo.get_item(item_id)
    assert item.state == ItemState.AWAITING_APPROVAL
    assert item.approved_by is None and item.approved_at is None
    assert not store.resolve_preview(item_id, 1, 7, True)
    assert item_id not in store.list_due_item_ids(now)
    auto = Projects((replace(project, approval="auto"),))
    assert store.save_reel(
        auto, replace(changed, approval="auto", fingerprint="auto")
    ) == ("refused", "the existing item has a different kind or approval mode")
    assert store.resolve_preview(item_id, 2, 7, True)
    repo.transition(item_id, ItemState.PUBLISHING)
    claim = repo.claim_publication(item_id, "threads")
    assert claim is not None
    repo.mark_published(item_id, "threads", "remote", claim=claim)
    assert (
        store.save_reel(projects, replace(reel, fingerprint="fourth"))[0] == "refused"
    )

    skipped = replace(reel, id=uuid4().hex)
    with conn.transaction():
        assert store.save_reel(projects, skipped) == ("new", None)
        skipped_id = store.preview_item_id(skipped)
        store.set_preview_message(skipped_id, 3)
    assert store.resolve_preview(skipped_id, 3, 7, False)
    assert repo.get_item(skipped_id).state == ItemState.SKIPPED
    assert store.save_reel(projects, replace(skipped, fingerprint="next")) == (
        "refused",
        "the existing item was skipped",
    )


def test_project_daily_count_uses_first_publication_and_berlin_day(conn):
    from postpilot.projects import Project, Projects
    from postpilot.store.project_items import ProjectItem, ProjectItemStore

    store = ProjectItemStore(conn)
    project = Project("quota", "owner/quota", ("reel",), ("threads",), approval="auto")
    projects = Projects((project,))
    berlin_midnight = datetime(2026, 12, 1, 23, tzinfo=UTC)
    reel = ProjectItem(
        "previous", berlin_midnight, False, "Text", "Thread", {}, 5, project.key
    )
    for name, first_published in [
        ("previous", berlin_midnight - timedelta(seconds=1)),
        ("current", berlin_midnight),
    ]:
        release = replace(reel, id=name)
        assert store.create_approved_reel(projects, release) == "new"
        with conn.transaction():
            item_id = store.preview_item_id(release)
            conn.execute(
                "INSERT INTO publications (item_id, channel, status, updated_at) VALUES "
                "(%s, 'threads', 'PUBLISHED', %s), (%s, 'instagram-reels', 'PUBLISHED', %s)",
                (
                    item_id,
                    first_published,
                    item_id,
                    berlin_midnight + timedelta(seconds=1),
                ),
            )
    assert (
        store.published_today(project.key, berlin_midnight - timedelta(seconds=1)) == 1
    )
    assert store.published_today(project.key, berlin_midnight) == 1
    assert store.published_today(project.key, berlin_midnight + timedelta(days=1)) == 0


@pytest.mark.parametrize("change_image", [False, True])
def test_project_post_save_replace_and_refuse_reel_clash(conn, repo, change_image):
    from hashlib import sha256

    from postpilot.projects import Project, Projects
    from postpilot.store.project_items import ProjectItem, ProjectItemStore

    def assets(text):
        data = text.encode()
        return {"post_de.txt": {"sha256": sha256(data).hexdigest(), "size": len(data)}}

    def image_asset(image):
        return {"image.png": {"sha256": sha256(image).hexdigest(), "size": len(image)}}

    original_text = "  Original.\n"
    project = Project("writer", "owner/writer", ("reel", "post"), ("linkedin-de",))
    projects = Projects((project,))
    store = ProjectItemStore(conn)
    release_id = "sample-image" if change_image else "sample"
    post = ProjectItem(
        release_id,
        datetime.now(UTC),
        True,
        original_text,
        None,
        assets(original_text) | (image_asset(b"old") if change_image else {}),
        None,
        project.key,
        approval="tap",
        kind="post",
        channels=("linkedin-de",),
        fingerprint="original",
    )
    assert store.save_item(projects, post) == ("new", None)
    with conn.transaction():
        item_id = store.preview_item_id(post)
        store.set_preview_message(item_id, 42)
    assert store.resolve_preview(item_id, 42, 7, True)
    assert store.save_item(projects, post) == ("unchanged", None)
    assert repo.get_item(item_id).state == ItemState.APPROVED
    text = original_text if change_image else "Revision"
    changed = replace(
        post,
        post_de=text,
        assets=assets(text) | (image_asset(b"new") if change_image else {}),
        fingerprint="revision",
    )
    assert store.save_item(projects, changed) == ("replaced", None)
    assert store.get_by_item(item_id) == replace(
        changed, item_id=item_id, threads_skipped=True
    )
    assert store.seen(project.key, f"post/{release_id}", "revision")
    assert repo.get_variants(item_id)[0].text == text
    assert repo.get_item(item_id).state == ItemState.AWAITING_APPROVAL
    assert store.save_item(projects, replace(changed, kind="reel", duration=10)) == (
        "refused",
        "the existing item has a different kind or approval mode",
    )
