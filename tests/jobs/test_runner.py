from unittest.mock import Mock

import pytest

from postpilot.config import Settings
from postpilot.jobs import runner


@pytest.fixture
def notifier(monkeypatch):
    notifier = Mock()
    monkeypatch.setattr(runner, "Notifier", Mock(return_value=notifier))
    return notifier


@pytest.fixture
def settings():
    return Settings("private-database", "private-telegram", 987654321, "private-key")


def test_success(settings, notifier):
    operation = Mock()
    assert runner.run_job("job", operation, settings) == 0
    operation.assert_called_once_with()
    notifier.send_alert.assert_not_called()


def test_failure_is_redacted(settings, notifier, capsys):
    fake_secret = "fake-secret-in-exception"
    error = ValueError(f"{fake_secret} {' '.join(settings.secret_values())}")
    assert runner.run_job("job", Mock(side_effect=error), settings) == 1
    stderr = capsys.readouterr().err
    assert fake_secret not in stderr
    assert stderr == "job failed (ValueError)\n"
    notifier.send_alert.assert_called_once()
    message = notifier.send_alert.call_args.args[0]
    assert "job failed (ValueError)" in message
    assert all(value not in message for value in settings.secret_values())


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_exit_and_interrupt_propagate(settings, notifier, error):
    with pytest.raises(error):
        runner.run_job("job", Mock(side_effect=error), settings)
    notifier.send_alert.assert_not_called()
