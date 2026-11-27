from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.rows import TupleRow

from postpilot.channels.paused import PAUSED_CHANNELS, is_paused
from postpilot.core.limits import LINKEDIN_TEXT_LIMIT
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
_PUBLICATION_COLUMNS = "item_id, channel, status, remote_id, error, updated_at, remote_ref, remote_ref_claimed_at"


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
    item_id, channel, status, remote_id, error, updated_at, ref, ref_claimed_at = row
    return Publication(
        item_id,
        channel,
        PublicationStatus(status),
        remote_id,
        error,
        updated_at,
        ref,
        ref_claimed_at,
    )


@dataclass(frozen=True)
class Claim:
    publication: Publication
    recovered: bool
    stale: bool = False

    @property
    def remote_ref(self) -> str | None:
        return self.publication.remote_ref


class LostPublicationClaim(IllegalTransition):
    pass


class EditTooLong(ValueError):
    pass


class _DuplicateInboxUpdate(Exception):
    pass


def _publication_table(channel: str) -> str:
    return "video_publications" if channel in {"youtube", "tiktok"} else "publications"


class ItemRepo:
    def __init__(self, conn: Connection[TupleRow]) -> None:
        self.conn = conn
        self.bot_state = BotState(conn)

    def create_item(self, source: Source) -> Item:
        if source == "project":
            raise ValueError("Project items must be created by project intake")
        with self.conn.transaction():
            row = self.conn.execute(
                f"INSERT INTO items (id, source, state) VALUES (%s, %s, %s) RETURNING {_ITEM_COLUMNS}",
                (uuid4(), source, ItemState.DRAFT.value),
            ).fetchone()
            assert row is not None
            return _item(row)

    def has_inbox_update(self, update_id: int) -> bool:
        with self.conn.transaction():
            return (
                self.conn.execute(
                    "SELECT 1 FROM inbox_updates WHERE update_id = %s", (update_id,)
                ).fetchone()
                is not None
            )

    def _record_inbox_update(self, update_id: int, item_id: UUID) -> None:
        result = self.conn.execute(
            "INSERT INTO inbox_updates (update_id, item_id) VALUES (%s, %s) "
            "ON CONFLICT (update_id) DO NOTHING",
            (update_id, item_id),
        )
        if not result.rowcount:
            raise _DuplicateInboxUpdate

    def create_inbox_item(
        self, update_id: int, original: str, translation: str
    ) -> Item | None:
        try:
            with self.conn.transaction():
                item = self.create_item("telegram")
                self._record_inbox_update(update_id, item.id)
                self.add_variant(item.id, "ua", original)
                self.add_variant(item.id, "de", translation)
                return self.transition(item.id, ItemState.AWAITING_APPROVAL)
        except _DuplicateInboxUpdate:
            return None

    def get_item(self, id: UUID) -> Item | None:
        with self.conn.transaction():
            row = self.conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM items WHERE id = %s", (id,)
            ).fetchone()
            return _item(row) if row is not None else None

    def list_unfinished_item_ids(self, *, source: Source | None = None) -> list[UUID]:
        # Resume items whose only failed publications are now paused.
        with self.conn.transaction():
            rows = self.conn.execute(
                """
                SELECT i.id FROM items i
                WHERE i.source <> 'project' AND (%s::text IS NULL OR i.source = %s)
                  AND (i.state = 'PUBLISHING' OR (
                    i.state = 'FAILED'
                    AND EXISTS (
                        SELECT 1 FROM item_publications p WHERE p.item_id = i.id
                        AND p.status = 'FAILED'
                        HAVING bool_and(p.channel = ANY(%s::text[]))
                    )
                  ))
                ORDER BY i.created_at, i.id
                """,
                (source, source, sorted(PAUSED_CHANNELS)),
            ).fetchall()
            return [item_id for (item_id,) in rows]

    def list_approved_item_ids(self, *, source: Source | None = None) -> list[UUID]:
        return self._list_item_ids(ItemState.APPROVED, source=source)

    def _list_item_ids(
        self, state: ItemState, *, source: Source | None = None
    ) -> list[UUID]:
        with self.conn.transaction():
            rows = self.conn.execute(
                "SELECT id FROM items WHERE state = %s AND source <> 'project' "
                "AND (%s::text IS NULL OR source = %s) "
                "ORDER BY created_at, id",
                (state.value, source, source),
            ).fetchall()
            return [item_id for (item_id,) in rows]

    def next_video_variant(self, *, retry: bool = False) -> Variant | None:
        with self.conn.transaction():
            row = self.conn.execute(
                """
                SELECT i.id, v.lang, v.text
                FROM items i JOIN variants v ON v.item_id = i.id AND v.lang = 'de'
                LEFT JOIN video_renders r ON r.item_id = i.id AND r.lang = v.lang
                WHERE i.state IN ('APPROVED', 'PUBLISHING', 'PUBLISHED')
                  AND i.source IN ('github', 'telegram')
                  AND CASE WHEN %s THEN r.status = 'FAILED' AND NOT r.retried
                           ELSE r.id IS NULL END
                ORDER BY i.created_at, i.id LIMIT 1
                """,
                (retry,),
            ).fetchone()
        return Variant(*row) if row is not None else None

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

    def update_variant_text(
        self,
        item_id: UUID,
        lang: Language,
        text: str,
        *,
        expected_state: ItemState = ItemState.AWAITING_APPROVAL,
    ) -> None:
        with self.conn.transaction():
            self._require_state(item_id, expected_state)
            updated = self.conn.execute(
                "UPDATE variants SET text = %s WHERE item_id = %s AND lang = %s",
                (text, item_id, lang),
            )
            if not updated.rowcount:
                raise KeyError((item_id, lang))

    def apply_pending_edit(
        self, user_id: int, text: str, *, update_id: int | None = None
    ) -> Item | None:
        try:
            with self.conn.transaction():
                pending = self.bot_state.take_pending_edit(user_id)
                if pending is None:
                    return None
                item_id, lang = pending
                if update_id is not None:
                    self._record_inbox_update(update_id, item_id)
                item = self.get_item(item_id)
                if (
                    item is not None
                    and item.source in {"telegram", "own_video"}
                    and len(text) > LINKEDIN_TEXT_LIMIT
                ):
                    raise EditTooLong
                try:
                    self.update_variant_text(item_id, lang, text)
                except IllegalTransition:
                    return None
                return self.get_item(item_id)
        except _DuplicateInboxUpdate:
            return None

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

    def resolve_failed_item(
        self, item_id: UUID, state: ItemState, message_id: int
    ) -> None:
        with self.conn.transaction():
            self.transition(item_id, state, expected_state=ItemState.FAILED)
            # Keep the last handled button message so a queued duplicate cannot
            # act on a later failure of the same item.
            handled = self.conn.execute(
                """
                INSERT INTO bot_state (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                WHERE bot_state.value::bigint < EXCLUDED.value::bigint
                """,
                (f"failed_item_message:{item_id}", str(message_id)),
            )
            if not handled.rowcount:
                raise IllegalTransition("Failure message already handled")

    def list_publications(self, item_id: UUID) -> list[Publication]:
        with self.conn.transaction():
            rows = self.conn.execute(
                f"SELECT {_PUBLICATION_COLUMNS} FROM item_publications WHERE item_id = %s ORDER BY channel",
                (item_id,),
            ).fetchall()
            return [_publication(row) for row in rows]

    def claim_publication(
        self, item_id: UUID, channel: str, *, stale_after: timedelta | None = None
    ) -> Claim | None:
        table = _publication_table(channel)
        with self.conn.transaction():
            state = self._lock_state(item_id)
            if state not in {ItemState.APPROVED, ItemState.PUBLISHING}:
                raise IllegalTransition(
                    f"Cannot claim a publication for an item in {state}"
                )
            row = self.conn.execute(
                f"""
                INSERT INTO {table} (item_id, channel, status, updated_at)
                VALUES (%s, %s, 'PUBLISHING', clock_timestamp())
                ON CONFLICT (item_id, channel) DO NOTHING
                RETURNING {_PUBLICATION_COLUMNS}
                """,
                (item_id, channel),
            ).fetchone()
            if row is not None:
                return Claim(_publication(row), recovered=False)

            previous = self.conn.execute(
                f"SELECT status FROM {table} WHERE item_id = %s AND channel = %s",
                (item_id, channel),
            ).fetchone()
            row = self.conn.execute(
                f"""
                UPDATE {table} SET status = 'PUBLISHING', error = NULL,
                    remote_id = NULL, updated_at = clock_timestamp(),
                    remote_ref = CASE WHEN status = 'PUBLISHING' OR %s THEN remote_ref END,
                    remote_ref_claimed_at = CASE WHEN status = 'PUBLISHING' OR %s
                        THEN remote_ref_claimed_at END
                WHERE item_id = %s AND channel = %s AND (
                    status = 'FAILED' OR (
                        status = 'PUBLISHING' AND
                        updated_at < clock_timestamp() - %s::interval
                    )
                )
                RETURNING {_PUBLICATION_COLUMNS}
                """,
                (
                    table == "video_publications",
                    table == "video_publications",
                    item_id,
                    channel,
                    stale_after,
                ),
            ).fetchone()
            if row is None:
                return None
            return Claim(
                _publication(row),
                recovered=True,
                stale=previous is not None
                and previous[0] == PublicationStatus.PUBLISHING,
            )

    def save_publication_ref(self, claim: Claim, remote_ref: str) -> None:
        publication = claim.publication
        table = _publication_table(publication.channel)
        with self.conn.transaction():
            self._lock_state(publication.item_id)
            result = self.conn.execute(
                f"""
                UPDATE {table} SET remote_ref = %s,
                    remote_ref_claimed_at = CASE WHEN remote_ref = %s
                        THEN remote_ref_claimed_at ELSE updated_at END
                WHERE item_id = %s AND channel = %s AND status = 'PUBLISHING'
                    AND updated_at = %s
                """,
                (
                    remote_ref,
                    remote_ref,
                    publication.item_id,
                    publication.channel,
                    publication.updated_at,
                ),
            )
            if result.rowcount == 0:
                raise LostPublicationClaim("Publication claim is no longer current")

    def release_publication(self, claim: Claim) -> None:
        publication = claim.publication
        table = _publication_table(publication.channel)
        with self.conn.transaction():
            self._lock_state(publication.item_id)
            self.conn.execute(
                f"""
                DELETE FROM {table}
                WHERE item_id = %s AND channel = %s
                  AND status = 'PUBLISHING' AND updated_at = %s
                """,
                (publication.item_id, publication.channel, publication.updated_at),
            )

    def finish_publishing(
        self, item_id: UUID, channels: set[str], *, held: frozenset[str] = frozenset()
    ) -> bool:
        """Return whether this call transitioned the item to FAILED."""
        with self.conn.transaction():
            if self._lock_state(item_id) != ItemState.PUBLISHING:
                return False
            statuses = {
                publication.channel: publication.status
                for publication in self.list_publications(item_id)
                if not is_paused(publication.channel)
            }
            if any(
                status == PublicationStatus.FAILED and channel not in held
                for channel, status in statuses.items()
            ):
                self.transition(item_id, ItemState.FAILED)
                return True
            if all(
                statuses.get(channel)
                in {PublicationStatus.PUBLISHED, PublicationStatus.SKIPPED}
                for channel in channels - PAUSED_CHANNELS
            ):
                self.transition(item_id, ItemState.PUBLISHED)
            return False

    def mark_published(
        self, item_id: UUID, channel: str, remote_id: str, *, claim: Claim | None = None
    ) -> None:
        self._finish_publication(
            item_id, channel, PublicationStatus.PUBLISHED, remote_id, None, claim
        )

    def mark_skipped(self, claim: Claim, error: str) -> None:
        publication = claim.publication
        self._finish_publication(
            publication.item_id,
            publication.channel,
            PublicationStatus.SKIPPED,
            None,
            error,
            claim,
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
        table = _publication_table(channel)
        with self.conn.transaction():
            claimed_at = None
            if claim is not None:
                self._lock_state(item_id)
                claimed_at = claim.publication.updated_at
            result = self.conn.execute(
                f"""
                UPDATE {table} SET status = %s, remote_id = %s, error = %s, updated_at = now()
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
