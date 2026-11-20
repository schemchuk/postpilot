import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, call

import httpx
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
        ("poll-engagement", True),
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


@pytest.mark.parametrize(
    "minute, result, full_run",
    [
        (5, [], False),
        (4, [], True),
        (15, [{"update_id": 42}], True),
        (15, None, True),
    ],
    ids=["empty", "hourly", "pending", "peek-error"],
)
def test_poll_telegram_dispatch(monkeypatch, capsys, minute, result, full_run):
    settings = Settings("fake-db", "fake-token", 123, "fake-key")
    monkeypatch.setattr(cli.Settings, "from_env", Mock(return_value=settings))
    steps = Mock()
    steps.connect.return_value = nullcontext("connection")
    if not full_run:
        steps.connect.side_effect = AssertionError("Unexpected database connection")
    monkeypatch.setattr(cli, "connect", steps.connect)
    monkeypatch.setattr(cli.migrate, "up", steps.up)
    monkeypatch.setitem(cli._DATABASE_COMMANDS, "poll-telegram", steps.job)

    def respond(request):
        assert request.url.path.endswith("/getUpdates")
        steps.peek(json.loads(request.content))
        if result is None:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json={"ok": True, "result": result})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(httpx, "post", client.post)
        assert (
            cli.main(
                ["poll-telegram"],
                now=lambda: datetime(2026, 11, 20, minute=minute, tzinfo=UTC),
            )
            == 0
        )
    expected = [call.peek({"offset": 0, "timeout": 0, "limit": 1})]
    if full_run:
        expected += [
            call.connect(settings.database_url),
            call.up("connection"),
            call.job(settings, "connection"),
        ]
        assert capsys.readouterr() == ("", "")
    else:
        assert capsys.readouterr() == (
            "poll-telegram: no pending updates, database not opened\n",
            "",
        )
    assert steps.mock_calls == expected


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
