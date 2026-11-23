import test_store

from postpilot.store.bot_state import BotState

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn


def test_linkedin_profile_round_trip(conn, database_schema):
    state = BotState(conn)
    assert state.get_linkedin_profile() is None
    assert state.set_linkedin_profile("Профіль\nLinkedIn") == 16
    assert state.set_linkedin_profile("GitHub", append=True) == 23

    with test_store.connect(test_store.DATABASE_URL, schema=database_schema) as fresh:
        other = BotState(fresh)
        assert other.get_linkedin_profile() == "Профіль\nLinkedIn\nGitHub"
        assert other.set_linkedin_profile("я" * 20_000) == 20_000
        assert state.set_linkedin_profile("overflow", append=True) is None
        assert state.set_linkedin_profile("я" * 20_001) is None
        assert state.get_linkedin_profile() == "я" * 20_000
        state.delete_linkedin_profile()
        assert other.get_linkedin_profile() is None


def test_imap_failure_counter_counts_and_resets(conn, database_schema):
    state = BotState(conn)
    state.reset_imap_failures()
    assert [state.record_imap_failure() for _ in range(3)] == [1, 2, 3]
    state.reset_imap_failures()
    assert state.record_imap_failure() == 1
