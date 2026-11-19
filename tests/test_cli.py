from contextlib import nullcontext
from unittest.mock import Mock, call

import pytest

from postpilot import cli
from postpilot.config import Settings


def test_no_command_shows_version_and_commands(capsys):
    assert cli.main([]) == 0
    output = capsys.readouterr().out
    assert f"postpilot {cli.__version__}" in output
    assert "check-tokens" in output


def test_unknown_command_exits_nonzero():
    with pytest.raises(SystemExit) as error:
        cli.main(["unknown-command"])
    assert error.value.code != 0


@pytest.mark.parametrize(
    "command, database",
    [
        ("draft-weekly", False),
        ("snapshot-traffic", False),
        ("poll-telegram", True),
        ("publish-due", True),
        ("check-tokens", True),
    ],
)
def test_dispatch(monkeypatch, command, database):
    settings = Settings("fake-db", "fake-token", 123, "fake-key")
    monkeypatch.setattr(cli.Settings, "from_env", Mock(return_value=settings))
    runner = Mock(return_value=7)
    monkeypatch.setattr(cli, "run_job", runner)
    connect = Mock(return_value=nullcontext("connection"))
    monkeypatch.setattr(cli, "connect", connect)
    steps = Mock()
    operation = steps.job
    monkeypatch.setattr(cli.migrate, "up", steps.up)
    monkeypatch.setattr(cli.tasks, "not_implemented", operation)
    monkeypatch.setattr(
        cli, "_DATABASE_COMMANDS", {command: operation} if database else {}
    )
    assert cli.main([command]) == 7
    name, job, received_settings = runner.call_args.args
    assert name == command and received_settings is settings
    job()
    if database:
        connect.assert_called_once_with(settings.database_url)
        assert steps.mock_calls == [
            call.up("connection"),
            call.job(settings, "connection"),
        ]
    else:
        connect.assert_not_called()
        assert steps.mock_calls == [call.job()]
