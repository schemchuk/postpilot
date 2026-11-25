import test_store

from postpilot.store.update_inbox import UpdateInbox

pytestmark = test_store.pytestmark
database_schema = test_store.database_schema
conn = test_store.conn


def test_update_inbox_lifecycle(conn):
    inbox = UpdateInbox(conn)
    older_pending = {"update_id": 4, "message": {"text": "Old pending"}}
    failed = {"update_id": 5}
    successful = {"update_id": 6}
    for update in (successful, failed, older_pending):
        assert inbox.insert(update)
    assert not inbox.insert({"update_id": 4, "message": {"text": "Duplicate"}})
    assert inbox.pending(limit=2) == [(4, older_pending), (5, failed)]
    assert inbox.pending() == [(4, older_pending), (5, failed), (6, successful)]

    assert inbox.pending(after_update_id=5) == [(6, successful)]

    inbox.mark_processed(6)
    assert inbox.record_failure(5) is False
    assert inbox.record_failure(5) is False
    assert inbox.pending() == [(4, older_pending), (5, failed)]
    assert inbox.record_failure(5) is True
    assert inbox.record_failure(5) is False
    assert inbox.pending() == [(4, older_pending)]

    with conn.transaction():
        conn.execute(
            "UPDATE telegram_inbox SET received_at = now() - interval '8 days'"
        )
        conn.execute(
            "UPDATE telegram_inbox SET processed_at = now() - interval '8 days' WHERE update_id = 5"
        )
    assert inbox.purge() == 1
    assert inbox.pending() == [(4, older_pending)]
    assert not inbox.insert(successful)
    assert inbox.insert(failed)
