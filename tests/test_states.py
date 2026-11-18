from itertools import product

import pytest

from postpilot.core.states import (
    IllegalTransition,
    ItemState,
    can_transition,
    ensure_transition,
)

ALLOWED = {
    ("DRAFT", "AWAITING_APPROVAL"),
    ("DRAFT", "SKIPPED"),
    ("AWAITING_APPROVAL", "APPROVED"),
    ("AWAITING_APPROVAL", "DRAFT"),
    ("AWAITING_APPROVAL", "SKIPPED"),
    ("APPROVED", "PUBLISHING"),
    ("APPROVED", "SKIPPED"),
    ("PUBLISHING", "PUBLISHED"),
    ("PUBLISHING", "FAILED"),
    ("FAILED", "PUBLISHING"),
    ("FAILED", "SKIPPED"),
}


@pytest.mark.parametrize(("source", "target"), list(product(ItemState, repeat=2)))
def test_transition_matrix(source, target):
    allowed = (source.value, target.value) in ALLOWED
    assert can_transition(source, target) is allowed
    if allowed:
        ensure_transition(source, target)
    else:
        with pytest.raises(IllegalTransition):
            ensure_transition(source, target)


def test_only_approved_and_failed_can_enter_publishing():
    assert {
        state for state in ItemState if can_transition(state, ItemState.PUBLISHING)
    } == {
        ItemState.APPROVED,
        ItemState.FAILED,
    }


@pytest.mark.parametrize("terminal", [ItemState.PUBLISHED, ItemState.SKIPPED])
def test_terminal_states_have_no_exits(terminal):
    assert not any(can_transition(terminal, target) for target in ItemState)
