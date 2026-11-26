from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest

from postpilot.core.models import Item
from postpilot.core.publisher import Publisher
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.projects import FACTORY_PROJECT, Project, Projects, ProjectsConfigError
from postpilot.store.project_items import ProjectItem, ProjectItemStore

REQUIRED = {
    "repo": '"owner/repo"',
    "kinds": '["reel"]',
    "channels": '["threads"]',
}
NOW = datetime(2026, 12, 1, 7, tzinfo=UTC)
PROJECT = Project("test", "owner/repo", ("reel",), ("threads",), "auto")
REEL = ProjectItem("clip", NOW, False, "text", "thread", {}, 10, project=PROJECT.key)


def config(tmp_path, values, key="test"):
    path = tmp_path / "projects.toml"
    path.write_text(
        f"[projects.{key}]\n" + "\n".join(f"{k} = {v}" for k, v in values.items())
    )
    return path


def test_defaults_and_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_FILE", str(config(tmp_path, REQUIRED)))
    registry = Projects.load()
    (project,) = registry
    assert project == Project("test", "owner/repo", ("reel",), ("threads",))
    assert registry.get("test") is project
    assert registry.by_repo("Owner/Repo") is project
    assert registry.get("missing") is registry.by_repo("owner/missing") is None
    with pytest.raises(FrozenInstanceError):
        project.enabled = False


@pytest.mark.parametrize("content", ["", "[projects]\n"])
def test_empty_projects(tmp_path, content):
    path = tmp_path / "projects.toml"
    path.write_text(content)
    assert list(Projects.load(path)) == []


def test_checkout_projects_from_another_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PROJECTS_FILE", raising=False)
    assert list(Projects.load()) == [
        Project(
            FACTORY_PROJECT,
            "schemchuk/beekeeper-ai-integrator",
            ("reel",),
            ("linkedin-de", "threads", "instagram", "facebook", "youtube", "tiktok"),
            "auto",
            2,
            True,
        )
    ]


def test_installed_cli_finds_config_from_checkout(monkeypatch, tmp_path):
    from postpilot import projects

    monkeypatch.chdir(projects.DEFAULT_PROJECTS_FILE.parents[1])
    monkeypatch.setattr(projects, "DEFAULT_PROJECTS_FILE", tmp_path / "missing.toml")
    monkeypatch.delenv("PROJECTS_FILE", raising=False)
    assert Projects.load().get(FACTORY_PROJECT) is not None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repo", None),
        ("repo", '"owner"'),
        ("repo", '"owner/repo/extra"'),
        ("repo", '"ow ner/repo"'),
        ("repo", "42"),
        ("kinds", None),
        ("kinds", "[]"),
        ("kinds", '["image"]'),
        ("kinds", '["reel", "reel"]'),
        ("kinds", '"reel"'),
        ("channels", None),
        ("channels", "[]"),
        ("channels", '["linkedin-ua"]'),
        ("channels", '["unknown"]'),
        ("channels", '["threads", "threads"]'),
        ("channels", "[1]"),
        ("approval", '"manual"'),
        ("max_per_day", "0"),
        ("max_per_day", "true"),
        ("max_per_day", "1.5"),
        ("enabled", '"true"'),
        ("extra", "true"),
    ],
)
def test_invalid_field(tmp_path, field, value):
    fields = REQUIRED.copy()
    if value is None:
        del fields[field]
    else:
        fields[field] = value
    with pytest.raises(ProjectsConfigError, match=rf"projects\.test\.{field}:"):
        Projects.load(config(tmp_path, fields))


@pytest.mark.parametrize("key", ["Test", "-test", "a" * 41, "under_score"])
def test_invalid_key(tmp_path, key):
    with pytest.raises(ProjectsConfigError, match=rf"projects\.{key}\.key:"):
        Projects.load(config(tmp_path, REQUIRED, key))


def test_duplicate_repo(tmp_path):
    path = config(tmp_path, REQUIRED)
    with path.open("a") as file:
        file.write(
            '\n[projects.other]\nrepo = "OWNER/repo"\nkinds = ["post"]\nchannels = ["threads"]\n'
        )
    with pytest.raises(ProjectsConfigError, match=r"projects\.other\.repo: duplicate"):
        Projects.load(path)


@pytest.mark.parametrize(
    ("project", "message"),
    [
        (None, "Unknown project"),
        (replace(PROJECT, enabled=False), "disabled"),
        (replace(PROJECT, approval="tap"), "needs a tap"),
        (replace(PROJECT, kinds=("post",)), "does not allow reels"),
    ],
)
def test_intake_refuses_unapproved_project_before_writes(project, message):
    registry = Projects((project,) if project else ())
    conn = Mock()
    with pytest.raises(IllegalTransition, match=message):
        ProjectItemStore(conn).create_approved_reel(registry, REEL)
    assert conn.mock_calls == []


@pytest.mark.parametrize(
    ("project", "approval", "approved_by", "message"),
    [
        (None, "auto", None, "Unknown project"),
        (replace(PROJECT, enabled=False), "auto", None, "disabled"),
        (replace(PROJECT, enabled=False), "tap", 123, "disabled"),
        (replace(PROJECT, approval="tap"), "auto", 123, "needs a tap"),
        (PROJECT, "tap", None, "needs a tap"),
        (replace(PROJECT, approval="tap"), "tap", None, "needs a tap"),
        (PROJECT, "auto", None, None),
        (PROJECT, "tap", 123, None),
        (replace(PROJECT, approval="tap"), "tap", 123, None),
    ],
)
def test_publish_checks_project_policy_and_channels(
    project, approval, approved_by, message
):
    registry = Projects((project,) if project else ())
    item = Item(uuid4(), "project", ItemState.APPROVED, NOW, NOW, approved_by, NOW)
    repo = Mock(
        **{
            "get_item.return_value": item,
            "transition.return_value": replace(item, state=ItemState.PUBLISHING),
            "get_variants.return_value": [],
            "list_publications.return_value": [],
            "finish_publishing.return_value": False,
        }
    )
    allowed = Mock(name="allowed")
    allowed.name = "threads"
    blocked = Mock(name="blocked")
    blocked.name = "youtube"
    publisher = Publisher(
        repo,
        [],
        project_channels=[allowed, blocked],
        project_item=lambda _: replace(REEL, approval=approval),
        projects=registry,
        now=lambda: NOW,
    )
    send = Mock(return_value="remote")
    publisher._send = send
    if message:
        with pytest.raises(IllegalTransition, match=message):
            publisher.publish_item(item.id)
        repo.transition.assert_not_called()
        repo.claim_publication.assert_not_called()
        send.assert_not_called()
    else:
        report = publisher.publish_item(item.id)
        assert {name: result.outcome for name, result in report.channels.items()} == {
            "threads": "published"
        }
        repo.transition.assert_called_once_with(item.id, ItemState.PUBLISHING)
        assert send.call_args.args[0] is allowed
        assert repo.finish_publishing.call_args.args == (item.id, {"threads"})
    assert blocked.mock_calls == []


def test_publish_batch_continues_after_policy_refusal(caplog):
    from postpilot.core.approval import ProjectApprovalError
    from postpilot.core.publisher import PublishReport
    from postpilot.jobs.tasks import _publish_items

    blocked, allowed = uuid4(), uuid4()
    publisher = Mock()
    publisher.publish_item.side_effect = [
        ProjectApprovalError("disabled"),
        PublishReport({}),
    ]
    _publish_items(publisher, [blocked, allowed])
    assert [call.args for call in publisher.publish_item.call_args_list] == [
        (blocked,),
        (allowed,),
    ]
    assert "disabled" in caplog.text
