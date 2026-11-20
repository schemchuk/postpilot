import json
from base64 import b64encode
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from postpilot.ai.client import AIError
from postpilot.ai.prompts import POST_SYSTEM_PROMPT, SERIES_SYSTEM_PROMPT
from postpilot.config import ConfigError, Settings, ShortioSettings
from postpilot.core.models import Item, Variant
from postpilot.core.states import ItemState
from postpilot.jobs import tasks
from postpilot.notify.telegram import TelegramError, render_preview
from postpilot.sources.series import Series, SeriesPart

NOW = datetime(2026, 11, 15, 23, 30, tzinfo=UTC)
WEEK = date(2026, 11, 16)
URL = "https://github.com/owner/alpha"
DE = "Ich habe den Test ergänzt.\n#Python #Tests #GitHub #CI"
UA = "Я дописав тест.\n#Python #Тести #GitHub #CI"
VERDICT = json.dumps(
    {"ai_score": 0, "passed": True, "issues": [], "unsupported_facts": []}
)


class ScriptedModel:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def complete(self, system, user, schema=None):
        self.calls.append((system, user, schema))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def job(monkeypatch):
    conn = Mock()
    conn.transaction.side_effect = nullcontext
    weekly = Mock()
    weekly.get.return_value = None
    series = Mock()
    series.get_active.return_value = None
    series.posted_repos.return_value = set()
    variants = {}
    item = Item(uuid4(), "github", ItemState.AWAITING_APPROVAL, NOW, NOW, None, None)

    def create(week, name, series_id, part, facts, de, ua):
        variants.update(de=de, ua=ua)
        return item

    weekly.create_drafted.side_effect = create
    repo = Mock()
    repo.list_approved_item_ids.return_value = []
    repo.list_unfinished_item_ids.return_value = []
    repo.get_variants.side_effect = lambda _: [
        Variant(item.id, lang, text) for lang, text in variants.items()
    ]
    repo.update_variant_text.side_effect = lambda _, lang, text: variants.update(
        {lang: text}
    )
    links = Mock()
    state = SimpleNamespace(
        conn=conn,
        repo=repo,
        weekly=weekly,
        series=series,
        variants=variants,
        item=item,
        links=links,
        messages=[],
        paths=[],
        active=["alpha", "beta"],
        license=True,
        shortio_status=200,
        telegram_status=200,
    )
    meta = {
        "name": "alpha",
        "html_url": URL,
        "description": "Tests",
        "language": "Python",
        "visibility": "public",
        "owner": {"login": "owner"},
        "pushed_at": NOW.isoformat(),
    }

    def respond(request):
        path = request.url.path
        if request.url.host == "api.telegram.org":
            state.messages.append(json.loads(request.content)["text"])
            return httpx.Response(
                state.telegram_status, json={"ok": True, "result": {"message_id": 1}}
            )
        if request.url.host == "api.short.io":
            body = json.loads(request.content)
            assert body["originalURL"] == URL
            lang = body["title"].split(" — ")[1]
            return httpx.Response(
                state.shortio_status,
                json={"idString": lang, "shortURL": f"https://short.test/{lang}"},
            )
        state.paths.append(path)
        if path == "/users/owner/repos":
            return httpx.Response(
                200, json=[{**meta, "name": name} for name in state.active]
            )
        if path.endswith("/commits"):
            commits = (
                [{"commit": {"message": "Add test"}}]
                if series.get_active.return_value is None
                else []
            )
            return httpx.Response(200, json=commits)
        if path.endswith("/license"):
            return httpx.Response(200 if state.license else 404)
        if path.endswith("/readme"):
            return httpx.Response(
                200,
                json={
                    "content": b64encode(b"x" * 1600).decode(),
                    "encoding": "base64",
                },
            )
        assert path == "/repos/owner/alpha"
        return httpx.Response(200, json=meta)

    client_factory = httpx.Client
    shortio_factory = tasks.ShortioClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        tasks.httpx,
        "Client",
        lambda **kwargs: client_factory(
            transport=kwargs.pop("transport", transport), **kwargs
        ),
    )
    monkeypatch.setattr(
        tasks,
        "ShortioClient",
        lambda settings: shortio_factory(settings, transport=transport),
    )
    monkeypatch.setattr(tasks, "WeeklyStore", lambda _: weekly)
    monkeypatch.setattr(tasks, "SeriesStore", lambda _: series)
    monkeypatch.setattr(tasks, "ItemRepo", lambda _: repo)
    monkeypatch.setattr(tasks, "LinkStore", lambda _: links)
    monkeypatch.setattr(tasks, "_now", lambda: NOW)
    state.model_factory = Mock(side_effect=AssertionError("Unexpected AI call"))
    monkeypatch.setattr(tasks, "ClaudeClient", state.model_factory)
    state.settings = Settings(
        "fake-db",
        "fake-bot",
        123,
        "fake-key",
        anthropic_api_key="fixture",
        github_token="fixture",
        github_owner="owner",
    )
    return state


def run(job):
    tasks.draft_weekly(job.settings, job.conn)


def script(job, draft=None, *responses):
    model = ScriptedModel(
        draft
        if draft is not None
        else json.dumps(
            {
                "decision": "post",
                "repo": "alpha",
                "post_de": DE,
                "post_uk": UA,
                "reason": "Tests",
            }
        ),
        *responses,
    )
    job.model_factory.side_effect = None
    job.model_factory.return_value = model
    return model


def start_series(job):
    series = Series(uuid4(), "alpha", 2, 3, WEEK, "active")
    job.series.get_active.return_value = series
    job.series.history.return_value = [
        SeriesPart(series.id, 1, "German", "Ukrainian", WEEK)
    ]
    return series


def test_rerun_uses_berlin_week_and_returns_before_io(job, capsys):
    job.weekly.get.return_value = SimpleNamespace(outcome="no_activity")
    run(job)
    job.weekly.get.assert_called_once_with(WEEK)
    job.model_factory.assert_not_called()
    assert job.paths == job.messages == []
    assert (
        capsys.readouterr().out.strip()
        == f"draft-weekly: week {WEEK} already handled (no_activity)"
    )


def test_preview_failure_leaves_saved_draft_and_retries_telegram_only(
    job, monkeypatch, capsys
):
    model = script(job, None, DE, VERDICT, UA, VERDICT)

    @contextmanager
    def commit_draft():
        yield
        args = job.weekly.create_drafted.call_args.args
        job.weekly.get.return_value = SimpleNamespace(
            outcome="drafted", item_id=job.item.id, facts=args[4]
        )
        job.repo.get_item.return_value = job.item

    job.conn.transaction.side_effect = commit_draft
    job.telegram_status = 500
    with pytest.raises(TelegramError):
        run(job)

    assert job.repo.get_item(job.item.id) == job.item
    assert job.weekly.get.return_value.facts["source_details"] == (
        "Source: alpha · 1 commits · link allowed"
    )
    assert job.variants == {"de": DE, "ua": UA}
    assert len(model.calls) == 5
    (first_preview,) = job.messages
    job.telegram_status = 200
    job.paths.clear()
    job.model_factory.side_effect = AssertionError("Unexpected AI call")
    shortio = Mock(side_effect=AssertionError("Unexpected Short.io call"))
    monkeypatch.setattr(tasks, "ShortioClient", shortio)
    job.settings = replace(
        job.settings, shortio=ShortioSettings("fixture", "short.test")
    )

    run(job)

    assert job.paths == []
    assert job.messages == [first_preview, first_preview]
    job.weekly.create_drafted.assert_called_once()
    shortio.assert_not_called()
    assert (
        capsys.readouterr().out.strip() == f"draft-weekly: week {WEEK} preview resent"
    )


def test_approved_draft_rerun_does_not_resend(job, capsys):
    job.weekly.get.return_value = SimpleNamespace(
        outcome="drafted", item_id=job.item.id
    )
    job.repo.get_item.return_value = replace(job.item, state=ItemState.APPROVED)

    run(job)

    job.model_factory.assert_not_called()
    assert job.paths == job.messages == []
    assert (
        capsys.readouterr().out.strip()
        == f"draft-weekly: week {WEEK} already handled (drafted)"
    )


def test_missing_ai_key_fails_before_github(job):
    job.settings = replace(job.settings, anthropic_api_key=None)
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        run(job)
    assert not job.paths


@pytest.mark.parametrize("excluded", [[], ["alpha", "beta"]])
def test_no_activity_reports_excluded_repos(job, excluded):
    job.active = excluded
    job.series.posted_repos.return_value = set(excluded)
    run(job)
    job.weekly.record_outcome.assert_called_once_with(
        WEEK, "no_activity", None, {"excluded_repos": excluded}
    )
    job.model_factory.assert_not_called()
    message = "No weekly post this week: no new public project with commits."
    if excluded:
        message += "\nActive, already posted: alpha, beta. Use /series to continue one."
    assert job.messages == [message]


@pytest.mark.parametrize("in_series", [False, True])
def test_generation_payload_and_preview(job, in_series):
    series = start_series(job) if in_series else None
    model = script(job, None, DE, VERDICT, UA, VERDICT)
    run(job)
    system, user, schema = model.calls[0]
    payload = json.loads(user)
    assert schema["properties"]["decision"]["enum"] == ["post", "skip"]
    if series:
        assert system == SERIES_SYSTEM_PROMPT
        assert payload["repo"]["name"] == "alpha"
        assert payload["recent_commits"] == []
        assert payload["previous_parts"] == [
            {"part": 1, "post_de": "German", "post_uk": "Ukrainian"}
        ]
        assert job.messages[0].endswith(
            "Source: alpha · part 2/3 · 0 commits · link allowed"
        )
    else:
        assert system == POST_SYSTEM_PROMPT
        assert payload["week"] == str(WEEK)
        assert [project["name"] for project in payload["projects"]] == ["alpha"]
        assert len(payload["projects"][0]["readme_excerpt"]) == 1500
        assert job.messages[0].endswith("Source: alpha · 1 commits · link allowed")
    assert job.variants == {"de": DE, "ua": UA}
    job.weekly.expire_older.assert_called_once_with(job.item.id)


@pytest.mark.parametrize("in_series", [False, True])
def test_model_skip(job, in_series):
    series = start_series(job) if in_series else None
    script(
        job,
        json.dumps(
            {
                "decision": "skip",
                "repo": "alpha",
                "post_de": "",
                "post_uk": "",
                "reason": "No story",
            }
        ),
    )
    run(job)
    week, outcome, name, facts = job.weekly.record_outcome.call_args.args
    assert (week, outcome, name, facts["reason"]) == (
        WEEK,
        "skipped",
        "alpha",
        "No story",
    )
    expected = "No weekly post this week (alpha): No story"
    if series:
        job.series.complete.assert_called_once_with(series_id=series.id)
        expected += "\nThe series about alpha ended."
    else:
        job.series.complete.assert_not_called()
    assert job.messages == [expected]
    job.weekly.create_drafted.assert_not_called()


@pytest.mark.parametrize(
    "draft",
    [
        "{",
        "[]",
        json.dumps(
            {
                "decision": "unknown",
                "repo": "alpha",
                "post_de": DE,
                "post_uk": UA,
                "reason": "why",
            }
        ),
        json.dumps(
            {
                "decision": "post",
                "repo": "beta",
                "post_de": DE,
                "post_uk": UA,
                "reason": "why",
            }
        ),
        json.dumps(
            {
                "decision": "post",
                "repo": "alpha",
                "post_de": " ",
                "post_uk": UA,
                "reason": "why",
            }
        ),
        json.dumps(
            {
                "decision": "post",
                "repo": "alpha",
                "post_de": "x" * 3001,
                "post_uk": UA,
                "reason": "why",
            }
        ),
    ],
)
def test_invalid_generation_creates_no_row(job, draft):
    script(job, draft)
    with pytest.raises(
        RuntimeError, match="Weekly draft generation or quality gate failed"
    ):
        run(job)
    job.weekly.create_drafted.assert_not_called()
    job.weekly.record_outcome.assert_not_called()


def test_humanizer_failure_creates_no_row(job):
    script(job, None, DE, VERDICT, AIError("sensitive draft text"))
    with pytest.raises(
        RuntimeError, match="Weekly draft generation or quality gate failed"
    ):
        run(job)
    job.weekly.create_drafted.assert_not_called()
    job.weekly.record_outcome.assert_not_called()


def draft_with_links():
    return json.dumps(
        {
            "decision": "post",
            "repo": "alpha",
            "post_de": f"{DE}\n{URL}",
            "post_uk": f"{UA}\n{URL}",
            "reason": "Tests",
        }
    )


def test_blocked_link_absent_from_payload_and_preview_text(job):
    job.license = False
    model = script(job, draft_with_links(), DE, VERDICT, UA, VERDICT)
    run(job)
    assert "url" not in json.loads(model.calls[0][1])["projects"][0]
    assert URL not in model.calls[1][1]
    assert URL not in job.messages[0]
    assert "link blocked: missing_license" in job.messages[0]


@pytest.mark.parametrize("status", [200, 500])
def test_shortio_attribution_and_fallback(job, status):
    job.shortio_status = status
    job.settings = replace(
        job.settings, shortio=ShortioSettings("fixture", "short.test")
    )
    script(job, draft_with_links(), f"{DE}\n{URL}", VERDICT, f"{UA}\n{URL}", VERDICT)
    run(job)
    saved = [call.args[0] for call in job.links.save_link.call_args_list]
    if status == 200:
        assert [(row.lang, row.item_id) for row in saved] == [
            ("de", job.item.id),
            ("ua", job.item.id),
        ]
        assert all(
            job.variants[lang].endswith(f"https://short.test/{lang}")
            for lang in ("de", "ua")
        )
    else:
        assert saved == []
        assert all(text.endswith(URL) for text in job.variants.values())


def test_preview_reserves_space_for_series_source(job):
    source = "Source: alpha · part 2/3 · 0 commits · link allowed"
    variants = [Variant(job.item.id, lang, "x" * 3000) for lang in ("de", "ua")]
    preview = render_preview(job.item, variants, source_details=source)
    assert len(preview) == 4096
    assert preview.endswith(source)
    assert "\n\nDE\n" in preview and "\n\nUA\n" in preview


def test_publish_due_records_only_pending_published_rows(job, monkeypatch):
    series_id, series_item, plain_item = uuid4(), uuid4(), uuid4()
    plain_week = date(2026, 11, 23)
    rows = {
        WEEK: SimpleNamespace(
            week=WEEK,
            item_id=series_item,
            repo="alpha",
            series_id=series_id,
            part=2,
            recorded_at=None,
        ),
        plain_week: SimpleNamespace(
            week=plain_week,
            item_id=plain_item,
            repo="beta",
            series_id=None,
            recorded_at=None,
        ),
    }
    job.weekly.get.side_effect = lambda week, **_: rows[week]
    job.weekly.list_unrecorded_published.side_effect = lambda: [
        row for row in rows.values() if row.recorded_at is None
    ]

    def mark(week):
        rows[week].recorded_at = NOW

    job.weekly.mark_recorded.side_effect = mark
    job.variants.update(de=DE, ua=UA)
    publisher = Mock()
    monkeypatch.setattr(tasks, "_publisher", lambda *_: publisher)
    publish = Mock(side_effect=RuntimeError("Channel publishing failed"))
    monkeypatch.setattr(tasks, "_publish_items", publish)
    with pytest.raises(RuntimeError, match="Channel publishing failed"):
        tasks.publish_due(job.settings, job.conn)
    publish.side_effect = None
    tasks.publish_due(job.settings, job.conn)
    job.series.record_part.assert_called_once_with(
        "alpha", 2, DE, UA, series_id=series_id
    )
    job.series.mark_posted.assert_called_once_with("beta")
    assert [call.args for call in job.weekly.mark_recorded.call_args_list] == [
        (WEEK,),
        (plain_week,),
    ]
