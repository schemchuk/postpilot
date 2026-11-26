from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypedDict
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from postpilot.core.approval import auto_approved_project
from postpilot.core.states import IllegalTransition
from postpilot.projects import Projects
from postpilot.store.repo import ItemRepo

type IntakeStatus = Literal["new", "replaced", "unchanged", "refused"]


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
                    duration, project, item_id, threads_skipped, title_de, approval, kind
                FROM project_items WHERE item_id = %s
                """,
                (item_id,),
            ).fetchone()
        return ProjectItem(*row) if row else None

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
        key = reel.project
        project = auto_approved_project(projects, key)
        if reel.kind != "reel" or "reel" not in project.kinds:
            raise IllegalTransition(f"Project {key!r} does not allow reels")
        with self.conn.transaction():
            # Serialize concurrent first inserts before there is a project item row to lock.
            self.conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext(%s))",
                (f"project:{key}:{reel.id}",),
            )
            previous = self.conn.execute(
                """
                SELECT f.item_id, f.release_at, f.ai, f.assets, f.title_de, f.kind, f.approval
                FROM project_items f
                JOIN items i ON i.id = f.item_id
                WHERE f.project = %s AND f.id = %s FOR UPDATE OF i
                """,
                (key, reel.id),
            ).fetchone()
            values = (
                reel.release_at,
                reel.ai,
                Jsonb(reel.assets),
                reel.post_de,
                reel.post_threads,
                reel.post_threads is None,
                reel.duration,
                reel.title_de,
            )
            repo = ItemRepo(self.conn)
            if previous is None:
                item_id = uuid4()
                self.conn.execute(
                    "INSERT INTO items (id, source, state, approved_at) "
                    "VALUES (%s, 'project', 'APPROVED', now())",
                    (item_id,),
                )
                self.conn.execute(
                    """
                    INSERT INTO project_items (project, kind, id, tag, item_id, approval, release_at, ai,
                        assets, post_de, post_threads, threads_skipped, duration, title_de)
                    VALUES (%s, 'reel', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (key, reel.id, reel.tag, item_id, project.approval, *values),
                )
                repo.add_variant(item_id, "de", reel.post_de)
                return "new"

            item_id, release_at, ai, assets, title_de, kind, approval = previous
            if kind != reel.kind or approval != project.approval:
                return "refused"
            if (release_at, ai, assets, title_de) == (
                reel.release_at,
                reel.ai,
                reel.assets,
                reel.title_de,
            ):
                return "unchanged"
            if self.conn.execute(
                "SELECT 1 FROM item_publications WHERE item_id = %s "
                "AND (status IN ('PUBLISHED', 'PUBLISHING') "
                "OR (channel = 'youtube' AND remote_ref IS NOT NULL)) LIMIT 1",
                (item_id,),
            ).fetchone():
                return "refused"
            self.conn.execute(
                """
                UPDATE project_items SET release_at = %s, ai = %s, assets = %s,
                    post_de = %s, post_threads = %s, threads_skipped = %s, duration = %s, title_de = %s,
                    updated_at = now()
                WHERE project = %s AND id = %s
                """,
                (*values, key, reel.id),
            )
            self.conn.execute(
                "UPDATE variants SET text = %s WHERE item_id = %s AND lang = 'de'",
                (reel.post_de, item_id),
            )
            return "replaced"
