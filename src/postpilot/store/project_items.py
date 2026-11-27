from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Literal, TypedDict
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from psycopg import Connection
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from postpilot.core.approval import auto_approved_project, enabled_project
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.projects import Projects
from postpilot.store.repo import ItemRepo
from postpilot.video.label import AI_IMAGE_CAPTION_LABEL, label_video_thread

type IntakeStatus = Literal["new", "replaced", "unchanged", "refused"]

POST_IMAGE_SIGNATURES = {
    "image.png": b"\x89PNG\r\n\x1a\n",
    "image.jpg": b"\xff\xd8\xff",
}


class Asset(TypedDict):
    sha256: str
    size: int


@dataclass(frozen=True)
class ProjectItem:
    id: str
    release_at: datetime
    ai: bool
    post_de: str
    post_threads: str | None
    assets: dict[str, Asset]
    duration: float | None
    project: str

    item_id: UUID | None = None
    threads_skipped: bool = False
    title_de: str | None = None

    approval: str = "auto"
    kind: str = "reel"
    channels: tuple[str, ...] | None = None
    fingerprint: str | None = field(default=None, compare=False)

    @property
    def image_name(self) -> str | None:
        if self.kind == "post":
            return next(
                (name for name in POST_IMAGE_SIGNATURES if name in self.assets), None
            )
        return None

    @property
    def image_caption_suffix(self) -> str:
        return "\n\n" + AI_IMAGE_CAPTION_LABEL if self.ai and self.image_name else ""

    @property
    def threads_text(self) -> str | None:
        if self.post_threads is None:
            return None
        if self.kind == "reel" and self.ai:
            return label_video_thread(self.post_threads)
        return self.post_threads + self.image_caption_suffix

    @property
    def tag(self) -> str:
        return f"{self.kind}/{self.id}"


class ProjectItemStore:
    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn

    def get_by_item(self, item_id: UUID) -> ProjectItem | None:
        with self.conn.transaction():
            row = self.conn.execute(
                """
                SELECT id, release_at, ai, post_de, post_threads, assets,
                    duration, project, item_id, threads_skipped, title_de, approval, kind, channels, fingerprint
                FROM project_items WHERE item_id = %s
                """,
                (item_id,),
            ).fetchone()
        if row is None:
            return None
        *fields, channels, fingerprint = row
        fields.extend((tuple(channels) if channels is not None else None, fingerprint))
        return ProjectItem(*fields)

    def list_due_item_ids(self, now: datetime) -> list[UUID]:
        with self.conn.transaction():
            rows = self.conn.execute(
                """
                SELECT f.item_id FROM project_items f JOIN items i ON i.id = f.item_id
                WHERE i.source = 'project' AND i.state IN ('APPROVED', 'PUBLISHING')
                    AND f.release_at <= %s
                ORDER BY f.release_at, f.item_id
                """,
                (now,),
            ).fetchall()
        return [item_id for (item_id,) in rows]

    @contextmanager
    def lock_item(self, item_id: UUID) -> Iterator[None]:
        with self.conn.transaction():
            self.conn.execute(
                "SELECT id FROM items WHERE id = %s FOR UPDATE", (item_id,)
            )
            yield

    def create_approved_reel(
        self, projects: Projects, reel: ProjectItem
    ) -> IntakeStatus:
        auto_approved_project(projects, reel.project)
        return self.save_reel(projects, reel)[0]

    def save_reel(
        self, projects: Projects, reel: ProjectItem
    ) -> tuple[IntakeStatus, str | None]:
        if reel.kind != "reel":
            raise IllegalTransition("Expected a reel")
        return self.save_item(projects, reel)

    def save_item(
        self, projects: Projects, item: ProjectItem
    ) -> tuple[IntakeStatus, str | None]:
        key = item.project
        project = enabled_project(projects, key)
        if item.kind not in project.kinds:
            raise IllegalTransition(f"Project {key!r} does not allow {item.kind}s")
        if item.approval != project.approval:
            raise IllegalTransition(f"Project {key!r} has a different approval mode")
        auto = project.approval == "auto"
        state = "APPROVED" if auto else "AWAITING_APPROVAL"
        channels = list(item.channels) if item.channels is not None else None
        with self.conn.transaction():
            # Serialize concurrent first inserts before there is a project item row to lock.
            self.conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext(%s))",
                (f"project:{key}:{item.id}",),
            )
            previous = self.conn.execute(
                """
                SELECT f.item_id, f.release_at, f.ai, f.assets, f.title_de, f.kind, f.approval,
                    f.channels, i.state
                FROM project_items f
                JOIN items i ON i.id = f.item_id
                WHERE f.project = %s AND f.id = %s FOR UPDATE OF i
                """,
                (key, item.id),
            ).fetchone()
            values = (
                item.release_at,
                item.ai,
                Jsonb(item.assets),
                item.post_de,
                item.post_threads,
                item.post_threads is None,
                item.duration,
                item.title_de,
                channels,
                item.fingerprint,
            )
            repo = ItemRepo(self.conn)
            if previous is None:
                item_id = uuid4()
                self.conn.execute(
                    "INSERT INTO items (id, source, state, approved_at) "
                    "VALUES (%s, 'project', %s, CASE WHEN %s THEN now() END)",
                    (item_id, state, auto),
                )
                self.conn.execute(
                    """
                    INSERT INTO project_items (project, kind, id, tag, item_id, approval, release_at, ai,
                        assets, post_de, post_threads, threads_skipped, duration, title_de, channels, fingerprint)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        key,
                        item.kind,
                        item.id,
                        item.tag,
                        item_id,
                        project.approval,
                        *values,
                    ),
                )
                repo.add_variant(item_id, "de", item.post_de)
                return "new", None

            (
                item_id,
                release_at,
                ai,
                assets,
                title_de,
                kind,
                approval,
                previous_channels,
                previous_state,
            ) = previous
            if kind != item.kind or approval != project.approval:
                return (
                    "refused",
                    "the existing item has a different kind or approval mode",
                )
            if item.fingerprint is not None and previous_state == "SKIPPED":
                return "refused", "the existing item was skipped"
            if (release_at, ai, assets, title_de, previous_channels) == (
                item.release_at,
                item.ai,
                item.assets,
                item.title_de,
                channels,
            ):
                if item.fingerprint is not None:
                    self.conn.execute(
                        "UPDATE project_items SET fingerprint = %s WHERE item_id = %s",
                        (item.fingerprint, item_id),
                    )
                return "unchanged", None
            if (
                item.fingerprint is not None and previous_state == "PUBLISHED"
            ) or self.conn.execute(
                "SELECT 1 FROM item_publications WHERE item_id = %s "
                "AND (status IN ('PUBLISHED', 'PUBLISHING') "
                "OR (channel = 'youtube' AND remote_ref IS NOT NULL)) LIMIT 1",
                (item_id,),
            ).fetchone():
                return "refused", "publishing has already started"
            self.conn.execute(
                """
                UPDATE project_items SET release_at = %s, ai = %s, assets = %s,
                    post_de = %s, post_threads = %s, threads_skipped = %s, duration = %s, title_de = %s,
                    channels = %s, fingerprint = %s, preview_message_id = NULL,
                    updated_at = now()
                WHERE project = %s AND id = %s
                """,
                (*values, key, item.id),
            )
            self.conn.execute(
                "UPDATE variants SET text = %s WHERE item_id = %s AND lang = 'de'",
                (item.post_de, item_id),
            )
            if item.fingerprint is not None:
                self.conn.execute(
                    "DELETE FROM publications WHERE item_id = %s AND status = 'FAILED'",
                    (item_id,),
                )
                self.conn.execute(
                    "DELETE FROM video_publications WHERE item_id = %s AND status IN ('FAILED', 'SKIPPED')",
                    (item_id,),
                )
            if not auto or (
                item.fingerprint is not None
                and previous_state in {"FAILED", "PUBLISHING"}
            ):
                self.conn.execute(
                    "UPDATE items SET state = %s, approved_by = NULL, "
                    "approved_at = CASE WHEN %s THEN now() END, updated_at = now() WHERE id = %s",
                    (state, auto, item_id),
                )
            return "replaced", None

    def seen(self, project: str, tag: str, fingerprint: str) -> bool:
        with self.conn.transaction():
            return (
                self.conn.execute(
                    """
                SELECT 1 FROM project_items WHERE project = %s AND tag = %s AND fingerprint = %s
                UNION ALL
                SELECT 1 FROM project_rejections WHERE project = %s AND tag = %s AND fingerprint = %s
                """,
                    (project, tag, fingerprint) * 2,
                ).fetchone()
                is not None
            )

    def record_rejection(
        self, project: str, tag: str, fingerprint: str, reason: str
    ) -> bool:
        with self.conn.transaction():
            return bool(
                self.conn.execute(
                    """
                INSERT INTO project_rejections (project, tag, fingerprint, reason)
                VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING
                """,
                    (project, tag, fingerprint, reason),
                ).rowcount
            )

    def preview_item_id(self, reel: ProjectItem) -> UUID:
        row = self.conn.execute(
            "SELECT item_id FROM project_items WHERE project = %s AND id = %s",
            (reel.project, reel.id),
        ).fetchone()
        assert row is not None
        return UUID(str(row[0]))

    def set_preview_message(self, item_id: UUID, message_id: int) -> None:
        self.conn.execute(
            "UPDATE project_items SET preview_message_id = %s WHERE item_id = %s",
            (message_id, item_id),
        )

    def resolve_preview(
        self, item_id: UUID, message_id: int, sender: int, approve: bool
    ) -> bool:
        with self.lock_item(item_id):
            row = self.conn.execute(
                """
                SELECT 1 FROM project_items f JOIN items i ON i.id = f.item_id
                WHERE f.item_id = %s AND f.preview_message_id = %s
                    AND f.approval = 'tap' AND i.state = 'AWAITING_APPROVAL'
                """,
                (item_id, message_id),
            ).fetchone()
            if row is None:
                return False
            ItemRepo(self.conn).transition(
                item_id,
                ItemState.APPROVED if approve else ItemState.SKIPPED,
                expected_state=ItemState.AWAITING_APPROVAL,
                approved_by=sender if approve else None,
            )
            return True

    def published_today(self, project: str, now: datetime) -> int:
        berlin = ZoneInfo("Europe/Berlin")
        day = now.astimezone(berlin).date()
        start = datetime.combine(day, time.min, berlin)
        end = datetime.combine(day + timedelta(days=1), time.min, berlin)
        with self.conn.transaction():
            row = self.conn.execute(
                """
                SELECT count(*) FROM (
                    SELECT min(p.updated_at) AS first_published
                    FROM project_items f JOIN item_publications p ON p.item_id = f.item_id
                    WHERE f.project = %s AND p.status = 'PUBLISHED'
                    GROUP BY f.item_id
                ) published WHERE first_published >= %s AND first_published < %s
                """,
                (project, start, end),
            ).fetchone()
        assert row is not None
        return int(row[0])
