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
from cryptography.fernet import Fernet
from psycopg import Error

from postpilot.ai import humanizer
from postpilot.ai.client import AIError
from postpilot.ai.prompts import POST_SYSTEM_PROMPT, SERIES_SYSTEM_PROMPT
from postpilot.config import ConfigError, Settings, ShortioSettings
from postpilot.core.models import Item, Variant
from postpilot.core.states import ItemState
from postpilot.jobs import alerts, tasks
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
    series.planned_part.return_value = None
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
    repo.update_variant_text.side_effect = lambda _, lang, text, **kwargs: (
        variants.update({lang: text})
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
        updates=[],
        telegram_calls=[],
        commit_counts=None,
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
            body = json.loads(request.content)
            state.telegram_calls.append((path.rsplit("/", 1)[-1], body))
            if path.endswith("/sendMessage"):
                state.messages.append(body["text"])
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
            if state.commit_counts is not None:
                assert request.url.params["per_page"] == "100"
                count = state.commit_counts[path.split("/")[-2]]
                return httpx.Response(
                    200,
                    json=[{"commit": {"message": f"Commit {i}"}} for i in range(count)],
                )
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
        assert path in {"/repos/owner/alpha", "/repos/owner/beta"}
        return httpx.Response(200, json={**meta, "name": path.split("/")[-1]})

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
    monkeypatch.setattr(httpx, "post", client_factory(transport=transport).post)
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
        Fernet.generate_key().decode(),
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


def untraced(capsys):
    lines = capsys.readouterr().out.splitlines()
    return "\n".join(line for line in lines if not line.startswith("trace ")).strip()


def start_series(job):
    series = Series(uuid4(), "alpha", 2, 3, WEEK, "active")
    job.series.get_active.return_value = series
    job.series.history.return_value = [
        SeriesPart(series.id, 1, "German", "Ukrainian", WEEK)
    ]
    return series


def test_rerun_uses_berlin_week_and_returns_before_io(job, capsys):
    job.weekly.get.return_value = SimpleNamespace(outcome="no_activity", item_id=None)
    run(job)
    job.weekly.get.assert_called_once_with(WEEK)
    job.model_factory.assert_not_called()
    assert job.paths == job.messages == []
    assert (
        untraced(capsys) == f"draft-weekly: week {WEEK} already handled (no_activity)"
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
    assert untraced(capsys) == f"draft-weekly: week {WEEK} preview resent"


def test_approved_draft_rerun_does_not_resend(job, capsys):
    job.weekly.get.return_value = SimpleNamespace(
        outcome="drafted", item_id=job.item.id
    )
    job.repo.get_item.return_value = replace(job.item, state=ItemState.APPROVED)

    run(job)

    job.model_factory.assert_not_called()
    assert job.paths == job.messages == []
    assert untraced(capsys) == f"draft-weekly: week {WEEK} already handled (drafted)"


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


@pytest.mark.parametrize(
    ("in_series", "queued"), [(False, False), (True, False), (True, True)]
)
def test_model_skip(job, in_series, queued):
    series = start_series(job) if in_series else None
    following = Series(uuid4(), "beta", 1, 5, WEEK, "active") if queued else None

    def complete(**_):
        job.series.get_active.return_value = following
        return series

    job.series.complete.side_effect = complete
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
        if queued:
            expected += "\nNext series started: beta."
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
    with pytest.raises(RuntimeError, match="Weekly draft failed:"):
        run(job)
    job.weekly.create_drafted.assert_not_called()
    job.weekly.record_outcome.assert_not_called()


def test_humanizer_failure_creates_no_row(job):
    script(job, None, DE, VERDICT, AIError("sensitive draft text"))
    with pytest.raises(RuntimeError, match="Weekly draft failed:"):
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
    assert (
        "\n\nDE\n" in preview
        and "\n\nUA (not published: LinkedIn UA paused)\n" in preview
    )


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


def response(**changes):
    return json.dumps(
        {
            "decision": "post",
            "repo": "alpha",
            "post_de": DE,
            "post_uk": UA,
            "reason": "sensitive draft text",
            **changes,
        }
    )


@pytest.mark.parametrize("series", [False, True])
def test_real_commit_count_and_capped_payload(job, series):
    if series:
        start_series(job)
    job.commit_counts = {"alpha": 21, "beta": 25}
    name = "alpha" if series else "beta"
    model = script(job, response(repo=name), DE, VERDICT, UA, VERDICT)
    run(job)
    payload = json.loads(model.calls[0][1])
    facts = job.weekly.create_drafted.call_args.args[4]
    assert json.loads(tasks.generation_message(facts["payload"])) == payload
    assert facts["prompt"] == ("series" if series else "post")
    assert facts["repo_url"] == f"https://github.com/owner/{name}"
    if series:
        count, commits = payload["commit_count"], payload["recent_commits"]
    else:
        (project,) = payload["projects"]
        assert project["name"] == "beta"
        count, commits = project["commit_count"], project["commits"]
    assert count == job.commit_counts[name]
    assert len(commits) == 20
    assert f"· {count} commits ·" in job.messages[0]


@pytest.mark.parametrize("prompt", [POST_SYSTEM_PROMPT, SERIES_SYSTEM_PROMPT])
def test_generation_prompt_requires_plain_paragraphs(prompt):
    assert "plain paragraphs with no bullet or numbered lists" in prompt
    assert "no lines starting with -, *, • or digits followed by a dot" in prompt
    assert "no em or en dashes" in prompt


MODEL_FAILURES = [
    ((AIError("sensitive draft text"),), "generation (AI request)"),
    (("{sensitive draft text",), "generation (invalid JSON)"),
    ((response(), DE, VERDICT, AIError("sensitive draft text")), "humanizer (ua)"),
    ((response(post_de="x" * 3001),), "length (de)"),
]


@pytest.mark.parametrize(
    ("scripted", "step"),
    [*MODEL_FAILURES, ((response(repo="wrong"),), "generation (wrong repo)")],
)
def test_draft_weekly_failure_alert_is_safe(job, scripted, step):
    script(job, *scripted)
    with pytest.raises(RuntimeError) as caught:
        run(job)
    alerts.report_failure("draft-weekly", caught.value, job.messages.append, (), None)
    assert job.messages == [
        f"draft-weekly failed (RuntimeError): Weekly draft failed: {step}"
    ]


@pytest.fixture
def regeneration(job, monkeypatch):
    source = "Source: alpha · part 2/3 · 21 commits · link allowed"
    payload = {"part": 2, "previous_parts": [], "recent_commits": ["A commit"]}
    job.model_factory.side_effect = None
    job.model_factory.return_value = ScriptedModel()
    job.row = SimpleNamespace(
        item_id=job.item.id,
        repo="alpha",
        facts={
            "payload": payload,
            "prompt": "series",
            "repo_url": URL,
            "link_allowed": True,
            "link_reasons": [],
            "source_details": source,
        },
    )
    job.source = source
    job.weekly.conn = job.conn
    job.weekly.get_by_item.return_value = job.row
    job.weekly.list_interrupted.return_value = []
    job.variants.update(de="Old German", ua="Old Ukrainian")
    job.repo.get_item.side_effect = lambda _: job.item
    job.inbox = Mock()
    job.inbox.pending.side_effect = lambda: [
        (update["update_id"], update) for update in job.updates
    ]
    monkeypatch.setattr(tasks, "UpdateInbox", Mock(return_value=job.inbox))

    def transition(item_id, state, *, expected_state, **kwargs):
        assert item_id == job.item.id and job.item.state == expected_state
        job.item = replace(job.item, state=state)
        return job.item

    @contextmanager
    def transaction():
        texts = job.variants.copy()
        item = job.item
        try:
            yield
        except Exception:
            job.variants.update(texts)
            job.item = item
            raise

    job.repo.transition.side_effect = transition
    job.conn.transaction.side_effect = transaction
    job.updates = [
        {
            "update_id": 20,
            "callback_query": {
                "from": {"id": job.settings.telegram_chat_id},
                "id": "regenerate",
                "data": f"regen:{job.item.id}",
                "message": {
                    "chat": {"id": job.settings.telegram_chat_id},
                    "message_id": 9,
                },
            },
        }
    ]
    return job


def poll(job):
    tasks.poll_telegram(job.settings, job.conn)
    assert job.item.state == ItemState.AWAITING_APPROVAL
    assert job.paths == []
    job.inbox.mark_processed.assert_any_call(20)


@pytest.mark.parametrize("status", [200, 500])
def test_regenerate_replaces_both_variants_and_redoes_tracking(regeneration, status):
    job = regeneration
    job.shortio_status = status
    job.settings = replace(
        job.settings, shortio=ShortioSettings("fixture", "short.test")
    )
    model = script(
        job, draft_with_links(), f"{DE}\n{URL}", VERDICT, f"{UA}\n{URL}", VERDICT
    )
    complete = model.complete

    def after_feedback(*args):
        assert [method for method, _ in job.telegram_calls[:2]] == [
            "answerCallbackQuery",
            "editMessageText",
        ]
        assert job.telegram_calls[1][1]["text"] == "Regenerating"
        assert job.item.state == ItemState.DRAFT
        return complete(*args)

    model.complete = after_feedback
    poll(job)
    expect_url = lambda lang: f"https://short.test/{lang}" if status == 200 else URL
    assert job.variants == {
        "de": f"{DE}\n{expect_url('de')}",
        "ua": f"{UA}\n{expect_url('ua')}",
    }
    links = [call.args[0] for call in job.links.save_link.call_args_list]
    assert [(link.lang, link.item_id) for link in links] == (
        [("de", job.item.id), ("ua", job.item.id)] if status == 200 else []
    )
    (preview,) = job.messages
    assert preview.endswith(job.source)
    assert all(text in preview for text in job.variants.values())
    assert model.calls[0][:2] == (
        SERIES_SYSTEM_PROMPT,
        tasks.generation_message(job.row.facts["payload"]),
    )


@pytest.mark.parametrize(
    ("scripted", "step"),
    [*MODEL_FAILURES, ((response(decision="skip"),), "model skip")],
)
def test_regeneration_failure_keeps_previous_preview(
    regeneration, scripted, step, caplog
):
    job = regeneration
    script(job, *scripted)
    job.updates.append(
        {
            "update_id": 20,
            "message": {
                "from": {"id": job.settings.telegram_chat_id},
                "chat": {"id": job.settings.telegram_chat_id},
                "text": "/series",
            },
        }
    )
    poll(job)
    assert job.variants == {"de": "Old German", "ua": "Old Ukrainian"}
    alert, preview, series_reply = job.messages
    assert alert == f"Regeneration failed, previous draft kept ({step})"
    assert "Old German" in preview and "Old Ukrainian" in preview
    assert preview.endswith(job.source)
    assert series_reply.startswith("No active series.")
    assert "sensitive draft text" not in caplog.text
    job.series.complete.assert_not_called()
    job.weekly.record_outcome.assert_not_called()


@pytest.mark.parametrize("failure", ["storage", "short links"])
def test_regenerate_rolls_back_replace_errors(regeneration, failure):
    job = regeneration
    script(job, draft_with_links(), f"{DE}\n{URL}", VERDICT, f"{UA}\n{URL}", VERDICT)
    if failure == "storage":
        update = job.repo.update_variant_text.side_effect

        def fail_second(item_id, lang, text, **kwargs):
            if lang == "ua":
                raise Error("private database detail")
            update(item_id, lang, text, **kwargs)

        job.repo.update_variant_text.side_effect = fail_second
    else:
        job.settings = replace(
            job.settings, shortio=ShortioSettings("fixture", "short.test")
        )
        job.links.save_link.side_effect = Error("private database detail")
    poll(job)
    assert job.variants == {"de": "Old German", "ua": "Old Ukrainian"}
    assert job.messages[0] == f"Regeneration failed, previous draft kept ({failure})"
    assert job.messages[1].endswith(job.source)


@pytest.mark.parametrize("series", [False, True])
def test_regenerates_legacy_flat_facts(regeneration, series):
    job = regeneration
    payload = {"previous_parts": []} if series else {"projects": [{"name": "alpha"}]}
    job.row.facts = {**payload, "link_allowed": True, "source_details": job.source}
    model = script(job, None, DE, VERDICT, UA, VERDICT)
    poll(job)
    assert model.calls[0][0] == (SERIES_SYSTEM_PROMPT if series else POST_SYSTEM_PROMPT)
    assert json.loads(model.calls[0][1]) == payload
    assert job.variants == {"de": DE, "ua": UA}


def test_regeneration_without_weekly_row(regeneration):
    job = regeneration
    job.weekly.get_by_item.return_value = None
    poll(job)
    assert job.messages == ["Regeneration is unavailable for this item"]


def test_poll_restores_interrupted_draft_before_processing_replayed_callback(
    regeneration,
):
    job = regeneration
    job.item = replace(job.item, state=ItemState.DRAFT)
    job.weekly.list_interrupted.return_value = [job.row]
    poll(job)
    alert, preview = job.messages
    assert alert == "Regeneration interrupted, previous draft kept"
    assert "Old German" in preview and preview.endswith(job.source)
    method, body = job.telegram_calls[-1]
    assert method == "answerCallbackQuery"
    assert body["text"] == "Already handled"
    job.weekly.get_by_item.assert_not_called()


def test_length_is_checked_after_humanizer(regeneration, monkeypatch):
    job = regeneration
    monkeypatch.setattr(humanizer, "MAX_CHARS", 4000)
    script(job, None, f"{DE} {'x' * 3001}", VERDICT, UA, VERDICT)
    poll(job)
    assert job.messages[0] == "Regeneration failed, previous draft kept (length (de))"
    assert job.variants == {"de": "Old German", "ua": "Old Ukrainian"}


@pytest.mark.parametrize("allowed", [True, False])
def test_planned_part_skips_ai_and_applies_link_policy(job, allowed, monkeypatch):
    series = start_series(job)
    job.settings = replace(job.settings, anthropic_api_key=None)
    job.series.planned_part.return_value = (f"  {DE}\n{URL}  ", f"{UA}\n{URL}\n")
    job.license = allowed
    humanize = Mock(side_effect=AssertionError("Unexpected humanizer call"))
    monkeypatch.setattr(tasks, "humanize_post", humanize)

    run(job)

    job.model_factory.assert_not_called()
    humanize.assert_not_called()
    job.series.planned_part.assert_called_once_with(series)
    job.series.history.assert_not_called()
    assert job.variants == {
        lang: f"{text}\n{URL}" if allowed else text
        for lang, text in (("de", DE), ("ua", UA))
    }
    status = "link allowed" if allowed else "link blocked: missing_license"
    source = f"Source: alpha · part 2/3 · planned text · 0 commits · {status}"
    assert job.weekly.create_drafted.call_args.args[4] == {
        "prompt": "planned",
        "repo_url": URL,
        "link_allowed": allowed,
        "link_reasons": () if allowed else ("missing_license",),
        "payload": {"repo": "alpha", "part": 2, "total_parts": 3},
        "source_details": source,
    }
    assert job.messages[0].endswith(source)


@pytest.mark.parametrize("step", ["length", "link validation"])
def test_invalid_planned_part_creates_no_row(job, monkeypatch, step):
    start_series(job)
    job.series.planned_part.return_value = ("x" * 3001, UA)
    if step == "link validation":
        monkeypatch.setattr(tasks, "finalize_links", Mock(side_effect=ValueError(DE)))
    with pytest.raises(RuntimeError) as caught:
        run(job)
    assert str(caught.value) == f"Weekly draft failed: {step} (de)"
    job.weekly.create_drafted.assert_not_called()
    job.weekly.record_outcome.assert_not_called()
    assert job.messages == []


@pytest.mark.parametrize("has_key", [True, False])
def test_regenerate_planned_part_resends_stored_preview(regeneration, has_key):
    job = regeneration
    if not has_key:
        job.settings = replace(job.settings, anthropic_api_key=None)
    job.row.facts["prompt"] = "planned"
    model = Mock()
    job.model_factory.return_value = model

    poll(job)

    model.complete.assert_not_called()
    alert, preview = job.messages
    assert alert == "Regeneration is unavailable for a planned part"
    assert preview.endswith(job.source)
    assert all(text in preview for text in job.variants.values())
    job.repo.update_variant_text.assert_not_called()


@pytest.mark.parametrize("in_series", [False, True])
def test_draft_traces_steps_without_post_text(job, capsys, in_series):
    if in_series:
        start_series(job)
    script(job, None, DE, VERDICT, UA, VERDICT)
    run(job)
    out = capsys.readouterr().out
    events = [line.split()[2] for line in out.splitlines() if line.startswith("trace ")]
    assert events[:4] == [
        "week",
        "series",
        "source" if in_series else "pick",
        "link-check",
    ]
    assert events[-2:] == ["drafted", "preview-sent"]
    assert f"item={job.item.id}" in out
    for text in (DE, UA):
        for line in text.splitlines():
            assert line not in out
    assert "fixture" not in out
