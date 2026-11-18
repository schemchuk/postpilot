from dataclasses import dataclass
from datetime import timedelta
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
from postpilot.store.bot_state import BotState

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


def _publication(row: TupleRow) -> Publication:
    item_id, channel, status, remote_id, error, updated_at = row
    return Publication(
        item_id, channel, PublicationStatus(status), remote_id, error, updated_at
    )


@dataclass(frozen=True)
class Claim:
    publication: Publication
    recovered: bool


class LostPublicationClaim(IllegalTransition):
    pass


class ItemRepo:
    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn
        self.bot_state = BotState(conn)

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

    def list_unfinished_item_ids(self) -> list[UUID]:
        with self.conn.transaction():
            rows = self.conn.execute(
                "SELECT id FROM items WHERE state = %s ORDER BY created_at, id",
                (ItemState.PUBLISHING.value,),
            ).fetchall()
            return [item_id for (item_id,) in rows]

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

    def update_variant_text(self, item_id: UUID, lang: Language, text: str) -> None:
        with self.conn.transaction():
            self._require_state(item_id, ItemState.AWAITING_APPROVAL)
            updated = self.conn.execute(
                "UPDATE variants SET text = %s WHERE item_id = %s AND lang = %s",
                (text, item_id, lang),
            )
            if not updated.rowcount:
                raise KeyError((item_id, lang))

    def apply_pending_edit(self, user_id: int, text: str) -> Item | None:
        with self.conn.transaction():
            pending = self.bot_state.take_pending_edit(user_id)
            if pending is None:
                return None
            item_id, lang = pending
            try:
                self.update_variant_text(item_id, lang, text)
            except IllegalTransition:
                return None
            return self.get_item(item_id)

    def _require_state(self, item_id: UUID, expected: ItemState) -> ItemState:
        state = self._lock_state(item_id)
        if state != expected:
            raise IllegalTransition(f"Expected {expected}, found {state}")
        return state

    def _lock_state(self, item_id: UUID) -> ItemState:
        row = self.conn.execute(
            "SELECT state FROM items WHERE id = %s FOR UPDATE", (item_id,)
        ).fetchone()
        if row is None:
            raise KeyError(item_id)
        return ItemState(row[0])

    def transition(
        self,
        item_id: UUID,
        new_state: ItemState,
        *,
        approved_by: int | None = None,
        expected_state: ItemState | None = None,
    ) -> Item:
        with self.conn.transaction():
            state = (
                self._lock_state(item_id)
                if expected_state is None
                else self._require_state(item_id, expected_state)
            )
            ensure_transition(state, new_state)
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

    def list_publications(self, item_id: UUID) -> list[Publication]:
        with self.conn.transaction():
            rows = self.conn.execute(
                f"SELECT {_PUBLICATION_COLUMNS} FROM publications WHERE item_id = %s ORDER BY channel",
                (item_id,),
            ).fetchall()
            return [_publication(row) for row in rows]

    def claim_publication(
        self, item_id: UUID, channel: str, *, stale_after: timedelta | None = None
    ) -> Claim | None:
        with self.conn.transaction():
            state = self._lock_state(item_id)
            if state not in {ItemState.APPROVED, ItemState.PUBLISHING}:
                raise IllegalTransition(
                    f"Cannot claim a publication for an item in {state}"
                )
            row = self.conn.execute(
                f"""
                INSERT INTO publications (item_id, channel, status, updated_at)
                VALUES (%s, %s, 'PUBLISHING', clock_timestamp())
                ON CONFLICT (item_id, channel) DO NOTHING
                RETURNING {_PUBLICATION_COLUMNS}
                """,
                (item_id, channel),
            ).fetchone()
            if row is not None:
                return Claim(_publication(row), recovered=False)

            row = self.conn.execute(
                f"""
                UPDATE publications SET status = 'PUBLISHING', error = NULL,
                    remote_id = NULL, updated_at = clock_timestamp()
                WHERE item_id = %s AND channel = %s AND (
                    status = 'FAILED' OR (
                        status = 'PUBLISHING' AND
                        updated_at < clock_timestamp() - %s::interval
                    )
                )
                RETURNING {_PUBLICATION_COLUMNS}
                """,
                (item_id, channel, stale_after),
            ).fetchone()
            return Claim(_publication(row), recovered=True) if row is not None else None

    def finish_publishing(self, item_id: UUID, channels: set[str]) -> None:
        with self.conn.transaction():
            if self._lock_state(item_id) != ItemState.PUBLISHING:
                return
            statuses = {
                publication.channel: publication.status
                for publication in self.list_publications(item_id)
            }
            if PublicationStatus.FAILED in statuses.values():
                self.transition(item_id, ItemState.FAILED)
            elif all(
                statuses.get(channel) == PublicationStatus.PUBLISHED
                for channel in channels
            ):
                self.transition(item_id, ItemState.PUBLISHED)

    def mark_published(
        self, item_id: UUID, channel: str, remote_id: str, *, claim: Claim | None = None
    ) -> None:
        self._finish_publication(
            item_id, channel, PublicationStatus.PUBLISHED, remote_id, None, claim
        )

    def mark_failed(
        self, item_id: UUID, channel: str, error: str, *, claim: Claim | None = None
    ) -> None:
        self._finish_publication(
            item_id, channel, PublicationStatus.FAILED, None, error, claim
        )

    def _finish_publication(
        self,
        item_id: UUID,
        channel: str,
        status: PublicationStatus,
        remote_id: str | None,
        error: str | None,
        claim: Claim | None,
    ) -> None:
        with self.conn.transaction():
            claimed_at = None
            if claim is not None:
                self._lock_state(item_id)
                claimed_at = claim.publication.updated_at
            result = self.conn.execute(
                """
                UPDATE publications SET status = %s, remote_id = %s, error = %s, updated_at = now()
                WHERE item_id = %s AND channel = %s AND status = 'PUBLISHING'
                    AND (%s::timestamptz IS NULL OR updated_at = %s)
                """,
                (
                    status.value,
                    remote_id,
                    error,
                    item_id,
                    channel,
                    claimed_at,
                    claimed_at,
                ),
            )
            if result.rowcount == 0:
                if claim is not None:
                    raise LostPublicationClaim("Publication claim is no longer current")
                raise IllegalTransition(
                    "Only a PUBLISHING publication can be completed"
                )
