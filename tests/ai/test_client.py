import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from postpilot.ai import client


@pytest.fixture
def fake_sdk(monkeypatch):
    calls = []
    response = SimpleNamespace(
        stop_reason="end_turn",
        content=[
            SimpleNamespace(type="thinking"),
            SimpleNamespace(type="text", text=" answer "),
        ],
    )

    def create(**kwargs):
        calls.append(kwargs)
        return response

    sdk = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setattr(client.anthropic, "Anthropic", lambda *, api_key: sdk)
    return calls, response


def test_client_structured_output(monkeypatch, fake_sdk):
    calls, _ = fake_sdk
    monkeypatch.setenv("CLAUDE_MODEL", "env-model")
    schema = {"type": "object"}
    assert client.ClaudeClient("fake").complete("system", "user", schema) == "answer"
    assert calls[0] == {
        "max_tokens": 4000,
        "model": "env-model",
        "system": "system",
        "messages": [{"role": "user", "content": "user"}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    client.ClaudeClient("fake", "explicit-model").complete("system", "user")
    assert calls[1]["model"] == "explicit-model"
    assert calls[1]["output_config"] is client.anthropic.omit
    monkeypatch.delenv("CLAUDE_MODEL")
    client.ClaudeClient("fake").complete("system", "user")
    assert calls[2]["model"] == "claude-sonnet-5-5"


@pytest.mark.parametrize("reason", ["max_tokens", "refusal", None])
def test_incomplete_response_raises(fake_sdk, reason):
    _, response = fake_sdk
    response.stop_reason = reason
    with pytest.raises(client.AIError, match="response stopped"):
        client.ClaudeClient("fake").complete("system", "user")


def test_sdk_is_imported_only_by_client():
    package = Path(__file__).resolve().parents[2] / "src" / "postpilot"
    for path in package.rglob("*.py"):
        if path == package / "ai" / "client.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                assert all(
                    alias.name.split(".")[0] != "anthropic" for alias in node.names
                ), path
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                assert (node.module or "").split(".")[0] != "anthropic", path
