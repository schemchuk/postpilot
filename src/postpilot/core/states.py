from enum import StrEnum


class ItemState(StrEnum):
    DRAFT = "DRAFT"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class IllegalTransition(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[ItemState, frozenset[ItemState]] = {
    ItemState.DRAFT: frozenset({ItemState.AWAITING_APPROVAL, ItemState.SKIPPED}),
    ItemState.AWAITING_APPROVAL: frozenset(
        {ItemState.APPROVED, ItemState.DRAFT, ItemState.SKIPPED}
    ),
    ItemState.APPROVED: frozenset({ItemState.PUBLISHING, ItemState.SKIPPED}),
    ItemState.PUBLISHING: frozenset({ItemState.PUBLISHED, ItemState.FAILED}),
    ItemState.PUBLISHED: frozenset(),
    ItemState.SKIPPED: frozenset(),
    ItemState.FAILED: frozenset({ItemState.PUBLISHING, ItemState.SKIPPED}),
}


def can_transition(a: ItemState, b: ItemState) -> bool:
    return b in ALLOWED_TRANSITIONS[a]


def ensure_transition(a: ItemState, b: ItemState) -> None:
    if not can_transition(a, b):
        raise IllegalTransition(f"Cannot transition from {a} to {b}")
