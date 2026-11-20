import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
SCHEDULES = {
    "poll-engagement": "*/10 * * * *",
    "poll-telegram": "*/5 * * * *",
    "check-tokens": "0 7 * * *",
    "snapshot-traffic": "30 7 * * *",
    "publish-due": "0 9 * * 5",
    "render-video": "0 10 * * 5",
    "publish-reels": "*/15 * * * *",
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
    dispatch = (
        {
            "inputs": {
                "retry": {
                    "description": (
                        "Retry one FAILED render (once per item)"
                        if name == "render-video"
                        else "Include FAILED publications when selecting one render"
                    ),
                    "type": "boolean",
                    "default": False,
                }
            }
        }
        if name in {"render-video", "publish-reels"}
        else None
    )
    assert triggers == {"schedule": [{"cron": cron}], "workflow_dispatch": dispatch}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {"group": name, "cancel-in-progress": False}
    for job in workflow["jobs"].values():
        assert "permissions" not in job
        assert job["timeout-minutes"] > 0
        if name in {"poll-engagement", "poll-telegram"}:
            assert job["timeout-minutes"] == 5
        assert {"uses": "actions/checkout@v4"} in job["steps"]
        assert {
            "uses": "actions/setup-python@v5",
            "with": {"python-version": "3.12"},
        } in job["steps"]
        assert any(
            step.get("run") == "python -m pip install ." for step in job["steps"]
        )
        expected_run = f"postpilot {name}"
        if dispatch is not None:
            expected_run += " ${{ inputs.retry && '--retry' || '' }}"
        command = next(step for step in job["steps"] if step.get("run") == expected_run)
        expected = {
            "DATABASE_URL",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "TOKEN_ENCRYPTION_KEY",
        }
        channel_credentials = {
            "LINKEDIN_CLIENT_ID",
            "LINKEDIN_CLIENT_SECRET",
            "LINKEDIN_REDIRECT_URI",
            "LINKEDIN_SCOPES",
            "THREADS_APP_ID",
            "THREADS_APP_SECRET",
        }
        if name == "snapshot-traffic":
            expected.update({"SHORTIO_API_KEY", "SHORTIO_DOMAIN", "GITHUB_TOKEN"})
        elif name == "publish-reels":
            assert job["timeout-minutes"] == 15
            expected.update({"FACEBOOK_PAGE_ID", "INSTAGRAM_BUSINESS_ACCOUNT_ID"})
        elif name == "render-video":
            assert job["timeout-minutes"] == 25
            assert any(
                "apt-get install -y ffmpeg fonts-dejavu-core" in step.get("run", "")
                for step in job["steps"][: job["steps"].index(command)]
            )
            expected.update(
                {
                    "HEYGEN_API_KEY",
                    "HEYGEN_AVATAR_ID",
                    "HEYGEN_VOICE_ID",
                    "VIDEO_MONTHLY_CAP",
                    "ANTHROPIC_API_KEY",
                }
            )
        elif name == "poll-engagement":
            expected.update(
                {
                    "ANTHROPIC_API_KEY",
                    "IMAP_USER",
                    "IMAP_PASSWORD",
                    "GITHUB_TOKEN",
                    "GITHUB_OWNER",
                }
            )
        else:
            expected.update(channel_credentials)
        if name == "check-tokens":
            expected.update({"FACEBOOK_PAGE_ID", "INSTAGRAM_BUSINESS_ACCOUNT_ID"})
        if name == "poll-telegram":
            expected.add("ANTHROPIC_API_KEY")
        assert set(command["env"]) == expected
        for variable, value in command["env"].items():
            if name == "poll-engagement" and variable in {
                "GITHUB_TOKEN",
                "GITHUB_OWNER",
            }:
                assert (
                    value
                    == {
                        "GITHUB_TOKEN": "${{ github.token }}",
                        "GITHUB_OWNER": "schemchuk",
                    }[variable]
                )
                continue
            secret = "GH_TRAFFIC_TOKEN" if variable == "GITHUB_TOKEN" else variable
            assert value == "${{ secrets." + secret + " }}"
