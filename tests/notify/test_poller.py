from unittest.mock import Mock, call

import pytest

from postpilot.notify.poller import drain_inbox


def test_drain_continues_past_failure_and_retries_on_next_run(caplog):
    first, second = {"update_id": 7}, {"update_id": 9}
    inbox = Mock()
    inbox.pending.side_effect = [[(7, first), (9, second)], [(7, first)]]
    inbox.record_failure.return_value = False
    handler = Mock()
    handler.handle_update.side_effect = [ValueError("private"), None, None]

    drain_inbox(inbox, handler)
    assert handler.handle_update.call_args_list == [call(first), call(second)]
    assert inbox.mock_calls == [
        call.pending(),
        call.record_failure(7),
        call.mark_processed(9),
        call.purge(7),
    ]

    drain_inbox(inbox, handler)
    handler.handle_update.assert_called_with(first)
    assert inbox.mock_calls[-3:] == [
        call.pending(),
        call.mark_processed(7),
        call.purge(7),
    ]
    assert caplog.messages == ["Telegram update handler failed (ValueError)"]


def test_drain_raises_first_error_after_purge_and_logs_dropped_update(caplog):
    failure = ValueError("private")
    inbox = Mock()
    inbox.pending.return_value = [(7, {}), (9, {})]
    inbox.record_failure.side_effect = [False, True]
    handler = Mock()
    handler.handle_update.side_effect = [failure, RuntimeError("also private")]

    with pytest.raises(ValueError) as caught:
        drain_inbox(inbox, handler, raise_errors=True)

    assert caught.value is failure
    inbox.mark_processed.assert_not_called()
    inbox.purge.assert_called_once_with(7)
    assert caplog.messages == [
        "Telegram update handler failed (ValueError)",
        "Telegram update handler failed (RuntimeError)",
        "Telegram update 9 dropped after 3 failures",
    ]


def test_drain_processes_later_batches_without_retrying_failures_in_same_run():
    inbox = Mock()
    inbox.pending.side_effect = [
        [(update_id, {"update_id": update_id}) for update_id in range(1, 101)],
        [(101, {"update_id": 101})],
    ]
    inbox.record_failure.return_value = False
    handler = Mock()
    handler.handle_update.side_effect = [ValueError(), *([None] * 100)]

    drain_inbox(inbox, handler)

    assert inbox.pending.call_args_list == [call(), call(after_update_id=100)]
    inbox.record_failure.assert_called_once_with(1)
    assert inbox.mark_processed.call_args_list == [
        call(update_id) for update_id in range(2, 102)
    ]
