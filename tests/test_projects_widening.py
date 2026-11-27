import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from postpilot.projects import Project, Projects, widenings

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_projects_widening.py"
PROJECT = Project("foo", "owner/repo", ("reel",), ("threads",), enabled=False)
AUTO = replace(PROJECT, approval="auto")
WIDE = replace(
    AUTO,
    channels=("threads", "tiktok", "youtube"),
    kinds=("reel", "post"),
    max_per_day=2,
    enabled=True,
    repo="owner/other",
)
TAP_CONFIG = """\
[projects.foo]
repo = "owner/repo"
kinds = ["reel"]
channels = ["threads"]
"""
AUTO_CONFIG = TAP_CONFIG + 'approval = "auto"\n'


@pytest.mark.parametrize(
    ("base", "head", "change"),
    [
        ((), (AUTO,), "approval: new auto project"),
        ((PROJECT,), (AUTO,), "approval: tap -> auto"),
        (
            (PROJECT,),
            (replace(PROJECT, channels=("threads", "tiktok")),),
            "channels: added tiktok",
        ),
        (
            (AUTO,),
            (replace(AUTO, channels=("threads", "tiktok")),),
            "channels: added tiktok",
        ),
        ((PROJECT,), (replace(PROJECT, kinds=("reel", "post")),), "kinds: added post"),
        ((PROJECT,), (replace(PROJECT, max_per_day=2),), "max_per_day: 1 -> 2"),
        ((AUTO,), (replace(AUTO, enabled=True),), "enabled: false -> true"),
        (
            (PROJECT,),
            (replace(PROJECT, repo=WIDE.repo),),
            "repo: owner/repo -> owner/other",
        ),
        ((WIDE,), (replace(WIDE, channels=("threads",)),), None),
        ((WIDE,), (replace(WIDE, kinds=("reel",)),), None),
        ((WIDE,), (replace(WIDE, max_per_day=1),), None),
        ((AUTO,), (PROJECT,), None),
        ((), (PROJECT,), None),
        ((AUTO,), (), None),
        (
            (WIDE,),
            (replace(WIDE, channels=WIDE.channels[::-1], kinds=WIDE.kinds[::-1]),),
            None,
        ),
        ((PROJECT,), (replace(PROJECT, repo="OWNER/Repo"),), None),
        ((PROJECT,), (replace(PROJECT, enabled=True),), None),
        ((WIDE,), (replace(WIDE, enabled=False),), None),
    ],
)
def test_widenings(base, head, change):
    assert widenings(Projects(base), Projects(head)) == (
        [f"projects.foo.{change}"] if change else []
    )


def test_widenings_order():
    base = Projects((PROJECT,))
    head = Projects(
        (replace(AUTO, key="zed"), replace(WIDE, channels=WIDE.channels[::-1]))
    )
    assert widenings(base, head) == [
        "projects.foo.approval: tap -> auto",
        "projects.foo.channels: added tiktok, youtube",
        "projects.foo.enabled: false -> true",
        "projects.foo.kinds: added post",
        "projects.foo.max_per_day: 1 -> 2",
        "projects.foo.repo: owner/repo -> owner/other",
        "projects.zed.approval: new auto project",
    ]


@pytest.mark.parametrize(
    "head_text", [TAP_CONFIG, "  # Policy\n\n" + TAP_CONFIG.replace(" = ", "  =  ")]
)
def test_equivalent_files(tmp_path, head_text):
    base = tmp_path / "base.toml"
    head = tmp_path / "head.toml"
    base.write_text(TAP_CONFIG)
    head.write_text(head_text)
    assert widenings(Projects.load(base), Projects.load(head)) == []


def run_check(base, head):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(base), str(head)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("base_text", "head_text", "status", "stdout"),
    [
        (TAP_CONFIG, AUTO_CONFIG, 1, "projects.foo.approval: tap -> auto\n"),
        (AUTO_CONFIG, TAP_CONFIG, 0, ""),
        (None, AUTO_CONFIG, 1, "projects.foo.approval: new auto project\n"),
        (None, TAP_CONFIG, 0, ""),
        (TAP_CONFIG, "[projects", 2, ""),
        (TAP_CONFIG, TAP_CONFIG + 'approval = "invalid"\n', 2, ""),
        ("[projects", AUTO_CONFIG, 2, ""),
        (TAP_CONFIG, None, 2, ""),
    ],
)
def test_script(tmp_path, base_text, head_text, status, stdout):
    base = tmp_path / "base.toml"
    head = tmp_path / "head.toml"
    if base_text is not None:
        base.write_text(base_text)
    if head_text is not None:
        head.write_text(head_text)
    result = run_check(base, head)
    assert result.returncode == status
    assert result.stdout == stdout
    if status == 2:
        assert "Invalid projects config:" in result.stderr
    else:
        assert result.stderr == ""


def test_script_real_config_unchanged():
    config = ROOT / "config/projects.toml"
    result = run_check(config, config)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
