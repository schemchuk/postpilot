from datetime import UTC, datetime

import pytest
import test_store

from postpilot.store.bot_state import BotState
from postpilot.tokens.oauth_state import InvalidState, make_state, verify_state

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn


def test_state_is_stored_and_consumed_once(conn):
    store = BotState(conn)
    now = datetime(2026, 11, 18, tzinfo=UTC)
    key = b"test key"
    state = make_state(key, "linkedin", now, store=store)
    with pytest.raises(InvalidState):
        verify_state(b"wrong key", state, now, store=store)
    assert verify_state(key, state, now, store=BotState(conn)) == "linkedin"
    with pytest.raises(InvalidState):
        verify_state(key, state, now, store=store)
