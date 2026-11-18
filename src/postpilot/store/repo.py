from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import TupleRow

from postpilot.core.models import (
    Item,
    Language,
    Publication,
    PublicationStatus,
    Source,
    Variant,
)
from postpilot.core.states import IllegalTransition, ItemState, ensure_transition

_ITEM_COLUMNS = "id, source, state, created_at, updated_at, approved_by, approved_at"
_PUBLICATION_COLUMNS = "item_id, channel, status, remote_id, error, updated_at"


def _item(row: TupleRow) -> Item:
    item_id, source, state, created_at, updated_at, approved_by, approved_at = row
    return Item(
        item_id,
        source,
        ItemState(state),
        created_at,
        updated_at,
        approved_by,
        approved_at,
    )


class ItemRepo:
    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn

    def create_item(self, source: Source) -> Item:
        with self.conn.transaction():
            row = self.conn.execute(
                f"INSERT INTO items (id, source, state) VALUES (%s, %s, %s) RETURNING {_ITEM_COLUMNS}",
                (uuid4(), source, ItemState.DRAFT.value),
            ).fetchone()
            assert row is not None
            return _item(row)

    def get_item(self, id: UUID) -> Item | None:
        with self.conn.transaction():
            row = self.conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM items WHERE id = %s", (id,)
            ).fetchone()
            return _item(row) if row is not None else None

    def add_variant(self, item_id: UUID, lang: Language, text: str) -> None:
        with self.conn.transaction():
            self.conn.execute(
                "INSERT INTO variants (item_id, lang, text) VALUES (%s, %s, %s)",
                (item_id, lang, text),
            )

    def get_variants(self, item_id: UUID) -> list[Variant]:
        with self.conn.transaction():
            rows = self.conn.execute(
                "SELECT item_id, lang, text FROM variants WHERE item_id = %s ORDER BY lang",
                (item_id,),
            ).fetchall()
            return [Variant(*row) for row in rows]

    def _lock_state(self, item_id: UUID) -> ItemState:
        row = self.conn.execute(
            "SELECT state FROM items WHERE id = %s FOR UPDATE", (item_id,)
        ).fetchone()
        if row is None:
            raise KeyError(item_id)
        return ItemState(row[0])

    def transition(
        self, item_id: UUID, new_state: ItemState, *, approved_by: int | None = None
    ) -> Item:
        with self.conn.transaction():
            ensure_transition(self._lock_state(item_id), new_state)
            if new_state == ItemState.APPROVED:
                if approved_by is None:
                    raise ValueError("approved_by is required for approval")
                row = self.conn.execute(
                    f"""
                    UPDATE items SET state = %s, updated_at = now(),
                        approved_by = %s, approved_at = now()
                    WHERE id = %s RETURNING {_ITEM_COLUMNS}
                    """,
                    (new_state.value, approved_by, item_id),
                ).fetchone()
            else:
                row = self.conn.execute(
                    f"UPDATE items SET state = %s, updated_at = now() WHERE id = %s RETURNING {_ITEM_COLUMNS}",
                    (new_state.value, item_id),
                ).fetchone()
            assert row is not None
            return _item(row)

    def claim_publication(self, item_id: UUID, channel: str) -> Publication | None:
        with self.conn.transaction():
            state = self._lock_state(item_id)
            if state not in {ItemState.APPROVED, ItemState.PUBLISHING}:
                raise IllegalTransition(
                    f"Cannot claim a publication for an item in {state}"
                )
            row = self.conn.execute(
                f"""
                INSERT INTO publications (item_id, channel, status)
                VALUES (%s, %s, 'PUBLISHING')
                ON CONFLICT (item_id, channel) DO NOTHING
                RETURNING {_PUBLICATION_COLUMNS}
                """,
                (item_id, channel),
            ).fetchone()
            if row is None:
                row = self.conn.execute(
                    f"""
                    UPDATE publications SET status = 'PUBLISHING', error = NULL,
                        remote_id = NULL, updated_at = now()
                    WHERE item_id = %s AND channel = %s AND status = 'FAILED'
                    RETURNING {_PUBLICATION_COLUMNS}
                    """,
                    (item_id, channel),
                ).fetchone()
            if row is None:
                return None
            ref, name, status, remote_id, error, updated_at = row
            return Publication(
                ref, name, PublicationStatus(status), remote_id, error, updated_at
            )

    def mark_published(self, item_id: UUID, channel: str, remote_id: str) -> None:
        self._finish_publication(
            item_id, channel, PublicationStatus.PUBLISHED, remote_id, None
        )

    def mark_failed(self, item_id: UUID, channel: str, error: str) -> None:
        self._finish_publication(
            item_id, channel, PublicationStatus.FAILED, None, error
        )

    def _finish_publication(
        self,
        item_id: UUID,
        channel: str,
        status: PublicationStatus,
        remote_id: str | None,
        error: str | None,
    ) -> None:
        with self.conn.transaction():
            result = self.conn.execute(
                """
                UPDATE publications SET status = %s, remote_id = %s, error = %s, updated_at = now()
                WHERE item_id = %s AND channel = %s AND status = 'PUBLISHING'
                """,
                (status.value, remote_id, error, item_id, channel),
            )
            if result.rowcount == 0:
                raise IllegalTransition(
                    "Only a PUBLISHING publication can be completed"
                )
