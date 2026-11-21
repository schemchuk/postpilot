import json
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock, call

import httpx
import pytest

from postpilot import cli
from postpilot.config import Settings
from postpilot.sources.series import ActiveSeriesExists


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
    "command",
    [
        "draft-weekly",
        "snapshot-traffic",
        "poll-engagement",
        "publish-due",
        "check-tokens",
        "threads-import-token",
        "facebook-import-token",
        "instagram-import-token",
    ],
)
def test_dispatch(monkeypatch, command):
    assert command in cli._DATABASE_COMMANDS
    settings = Settings("fake-db", "fake-token", 123, "fake-key")
    monkeypatch.setattr(cli.Settings, "from_env", Mock(return_value=settings))
    runner = Mock(return_value=7)
    monkeypatch.setattr(cli, "run_job", runner)
    connect = Mock(return_value=nullcontext("connection"))
    monkeypatch.setattr(cli, "connect", connect)
    steps = Mock()
    operation = steps.job
    monkeypatch.setattr(cli.migrate, "up", steps.up)
    monkeypatch.setitem(cli._DATABASE_COMMANDS, command, operation)
    assert cli.main([command]) == 7
    name, job, received_settings = runner.call_args.args
    assert name == command and received_settings is settings
    job()
    connect.assert_called_once_with(settings.database_url)
    assert steps.mock_calls == [
        call.up("connection"),
        call.job(settings, "connection"),
    ]


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


@pytest.fixture
def series_import(monkeypatch, tmp_path):
    settings = Settings("fake-db", "fake-token", 123, "fake-key")
    monkeypatch.setattr(cli.Settings, "from_env", Mock(return_value=settings))
    steps = Mock()
    steps.connect.return_value = nullcontext("connection")
    steps.store.return_value.import_series = steps.import_series
    monkeypatch.setattr(cli, "connect", steps.connect)
    monkeypatch.setattr(cli.migrate, "up", steps.up)
    monkeypatch.setattr(cli, "SeriesStore", steps.store)
    monkeypatch.setattr(
        cli, "run_job", Mock(side_effect=AssertionError("Unexpected job runner"))
    )
    data = {
        "repo": "handmake",
        "part": 3,
        "total_parts": 5,
        "started_on": "2026-11-15",
        "published": [
            {
                "part": 1,
                "text_de": "Private post",
                "text_ua": "Один",
                "published_on": "2026-11-16",
            }
        ],
        "planned": [{"part": 3, "text_de": "Drei", "text_ua": "Три"}],
        "posted_repos": ["Handmake", "work_in_Linkedin"],
    }
    return tmp_path / "series.json", data, steps


def test_series_import_command(series_import, capsys):
    path, data, steps = series_import
    path.write_text(json.dumps(data), encoding="utf-8")
    assert cli.main(["series-import", str(path)]) == 0
    assert steps.mock_calls == [
        call.connect("fake-db"),
        call.up("connection"),
        call.store("connection"),
        call.import_series(
            repo="handmake",
            part=3,
            total_parts=5,
            started_on=date(2026, 11, 15),
            published=[(1, "Private post", "Один", date(2026, 11, 16))],
            planned=[(3, "Drei", "Три")],
            posted_repos=["Handmake", "work_in_Linkedin"],
        ),
    ]
    assert capsys.readouterr().out == (
        "series-import: handmake part 3/5, 1 published, 1 planned, 2 posted repos\n"
    )


def test_series_import_requires_path(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["series-import"])
    assert caught.value.code != 0
    assert "path" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("repo", " ", "repo must be"),
        ("part", 6, "part and total_parts"),
        ("part", True, "part and total_parts"),
        ("total_parts", "5", "part and total_parts"),
        ("started_on", "2026-02-30", "started_on must be"),
        ("published", [{}], "published[0].part"),
        ("published", None, "published must be an array"),
        ("posted_repos", [False], "posted_repos must contain"),
        ("planned", [{"part": 2}], "planned[0].part"),
        ("planned", [{"part": 6}], "planned[0].part"),
        ("planned", [{"part": 3, "text_de": "x" * 3001}], "planned[0].text_de"),
        (
            "planned",
            [{"part": 3, "text_de": "Private post", "text_ua": " "}],
            "planned[0].text_ua",
        ),
        (
            "published",
            [
                {
                    "part": 1,
                    "text_de": "Eins",
                    "text_ua": "Один",
                    "published_on": "Friday",
                }
            ],
            "published[0].published_on",
        ),
    ],
)
def test_series_import_rejects_invalid_values(
    series_import, capsys, field, value, message
):
    path, data, steps = series_import
    path.write_text(json.dumps({**data, field: value}), encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        cli.main(["series-import", str(path)])
    assert caught.value.code != 0
    stderr = capsys.readouterr().err
    assert message in stderr and "Private post" not in stderr
    assert steps.mock_calls == []


@pytest.mark.parametrize("field", ["published", "planned"])
def test_series_import_rejects_duplicate_parts(series_import, capsys, field):
    path, data, steps = series_import
    data[field] *= 2
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.main(["series-import", str(path)])
    assert f"{field}[1].part must be unique" in capsys.readouterr().err
    assert steps.mock_calls == []


def test_series_import_rejects_malformed_json(series_import, capsys):
    path, _, steps = series_import
    path.write_text('{"text_de": "Private post', encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.main(["series-import", str(path)])
    stderr = capsys.readouterr().err
    assert "valid UTF-8 JSON" in stderr and "Private post" not in stderr
    assert steps.mock_calls == []


def test_series_import_reports_active_series(series_import, capsys):
    path, data, steps = series_import
    path.write_text(json.dumps(data), encoding="utf-8")
    steps.import_series.side_effect = ActiveSeriesExists("A series is already active.")
    with pytest.raises(SystemExit) as caught:
        cli.main(["series-import", str(path)])
    assert caught.value.code != 0
    out, err = capsys.readouterr()
    assert not out and "A series is already active." in err
