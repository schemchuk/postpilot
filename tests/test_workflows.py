import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
SCHEDULES = {
    "poll-telegram": "*/5 * * * *",
    "check-tokens": "0 7 * * *",
    "publish-due": "0 9 * * 5",
}


def check_secret_placement(node, in_env=False):
    if isinstance(node, dict):
        for key, value in node.items():
            check_secret_placement(key, in_env)
            check_secret_placement(
                value, in_env or (key == "env" and isinstance(value, dict))
            )
    elif isinstance(node, list):
        for value in node:
            check_secret_placement(value, in_env)
    elif isinstance(node, str) and not in_env:
        assert not re.search(r"\bsecrets\s*(?:\.|\[)", node, re.IGNORECASE)


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")))
def test_workflow_common_safety(path):
    workflow = yaml.safe_load(path.read_text())
    triggers = workflow["on"] if "on" in workflow else workflow[True]
    assert "pull_request_target" not in triggers
    check_secret_placement(workflow)


@pytest.mark.parametrize("name,cron", SCHEDULES.items())
def test_scheduled_workflow(name, cron):
    workflow = yaml.safe_load((WORKFLOWS / f"{name}.yml").read_text())
    triggers = workflow["on"] if "on" in workflow else workflow[True]
    assert triggers == {"schedule": [{"cron": cron}], "workflow_dispatch": None}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {"group": name, "cancel-in-progress": False}
    for job in workflow["jobs"].values():
        assert "permissions" not in job
        assert job["timeout-minutes"] > 0
        if name == "poll-telegram":
            assert job["timeout-minutes"] == 5
        assert {"uses": "actions/checkout@v4"} in job["steps"]
        assert {
            "uses": "actions/setup-python@v5",
            "with": {"python-version": "3.12"},
        } in job["steps"]
        assert any(
            step.get("run") == "python -m pip install ." for step in job["steps"]
        )
        command = next(
            step for step in job["steps"] if step.get("run") == f"postpilot {name}"
        )
        expected = {
            "DATABASE_URL",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "TOKEN_ENCRYPTION_KEY",
            "LINKEDIN_CLIENT_ID",
            "LINKEDIN_CLIENT_SECRET",
            "LINKEDIN_REDIRECT_URI",
            "LINKEDIN_SCOPES",
            "THREADS_APP_ID",
            "THREADS_APP_SECRET",
        }
        if name == "poll-telegram":
            expected.add("ANTHROPIC_API_KEY")
        assert set(command["env"]) == expected
        for variable, value in command["env"].items():
            assert value == "${{ secrets." + variable + " }}"
