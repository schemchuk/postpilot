from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
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
        ("snapshot-traffic", True),
        ("poll-telegram", True),
        ("publish-due", True),
        ("check-tokens", True),
        ("threads-import-token", True),
        ("facebook-import-token", True),
        ("instagram-import-token", True),
    ],
)
def test_dispatch(monkeypatch, command, database):
    assert (command in cli._DATABASE_COMMANDS) == database
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


def test_threads_import_uses_environment_and_stores_expiry(monkeypatch, capsys):
    now = datetime(2026, 11, 19, tzinfo=UTC)
    settings = Settings("fake-db", "fake-bot-token", 123, "fake-key")
    store = Mock()
    monkeypatch.setattr(cli.tasks, "TokenStore", Mock(return_value=store))
    monkeypatch.setattr(cli.tasks, "TokenCipher", Mock())
    monkeypatch.setattr(cli.tasks, "_now", lambda: now)
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "mock-import-token")
    cli.tasks.threads_import_token(settings, Mock())
    store.save.assert_called_once_with(
        "threads", "mock-import-token", now + timedelta(days=60)
    )
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("command", ["render-video", "publish-reels"])
def test_retryable_command_dispatch(monkeypatch, command, retry):
    from postpilot.config import HeyGenSettings

    settings = Settings(
        "fake-db",
        "fake-token",
        123,
        "fake-key",
        heygen=HeyGenSettings("key", "avatar", "voice"),
        anthropic_api_key="fake-model-key",
    )
    monkeypatch.setattr(cli.Settings, "from_env", Mock(return_value=settings))
    monkeypatch.setattr(cli, "connect", Mock(return_value=nullcontext("connection")))
    steps = Mock()
    monkeypatch.setattr(cli.migrate, "up", steps.up)
    monkeypatch.setattr(cli.tasks, command.replace("-", "_"), steps.job)
    runner = Mock(side_effect=lambda name, job, settings: job() or 0)
    monkeypatch.setattr(cli, "run_job", runner)

    assert cli.main([command, *(["--retry"] if retry else [])]) == 0
    assert steps.mock_calls == [
        call.up("connection"),
        call.job(settings, "connection", retry=retry),
    ]


@pytest.mark.parametrize("has_heygen", [False, True])
def test_render_video_missing_config(monkeypatch, capsys, has_heygen):
    from postpilot.config import HeyGenSettings

    settings = Settings(
        "fake-db",
        "fake-token",
        123,
        "fake-key",
        heygen=HeyGenSettings("key", "avatar", "voice") if has_heygen else None,
    )
    monkeypatch.setattr(cli.Settings, "from_env", Mock(return_value=settings))
    connect = Mock()
    monkeypatch.setattr(cli, "connect", connect)
    with pytest.raises(SystemExit) as error:
        cli.main(["render-video"])
    assert error.value.code == 2
    assert (
        "ANTHROPIC_API_KEY" if has_heygen else "HEYGEN_API_KEY"
    ) in capsys.readouterr().err
    connect.assert_not_called()
