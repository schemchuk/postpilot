from datetime import UTC, datetime, timedelta

import pytest

from postpilot.tokens.oauth_state import InvalidState, make_state, verify_state

NOW = datetime(2026, 11, 18, tzinfo=UTC)
KEY = b"test key"


def test_state_verifies():
    state = make_state(KEY, "linkedin", NOW)
    assert verify_state(KEY, state, NOW + timedelta(minutes=5)) == "linkedin"


@pytest.mark.parametrize(
    ("key", "age"),
    [(b"other key", 0), (KEY, 1801), (KEY, -1)],
)
def test_wrong_key_expired_or_future(key, age):
    state = make_state(KEY, "linkedin", NOW)
    with pytest.raises(InvalidState):
        verify_state(key, state, NOW + timedelta(seconds=age))


@pytest.mark.parametrize(
    "change",
    [
        lambda state: state.replace("linkedin", "threads"),
        lambda state: state[:-1] + ("0" if state[-1] != "0" else "1"),
        lambda state: state.rsplit(".", 1)[0],
        lambda state: f"{state}.extra",
        lambda _: "☃",
    ],
)
def test_tampered_or_malformed_state(change):
    with pytest.raises(InvalidState):
        verify_state(KEY, change(make_state(KEY, "linkedin", NOW)), NOW)
