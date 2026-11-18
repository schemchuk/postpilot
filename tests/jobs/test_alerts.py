import pytest

from postpilot.jobs.alerts import github_run_url, redact, report_failure


@pytest.mark.parametrize(
    "value",
    [
        "123456789:" + "aB9_-" * 7,
        "postgres://example:private@example.test/db",
        "postgresql://example:private@example.test/db",
        "Bearer short-value",
        "f0" * 16,
        "Ab5+/c9_" * 5 + "=",
        "known secret & value",
    ],
)
def test_redacts_shapes_and_known_values(value):
    assert (
        redact(f"Error: {value} failed", ["known secret & value"])
        == "Error: *** failed"
    )


def test_overlapping_redactions_hide_entire_secret():
    assert redact("prefix-secret suffix", ["prefix-secret", "secret suffix"]) == "***"


def test_report_includes_run_url_and_caps_message(monkeypatch):
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.example.test/")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    url = github_run_url()
    assert url == "https://github.example.test/owner/repo/actions/runs/42"
    messages = []
    report_failure("publish-due", ValueError("long " * 200), messages.append, [], url)
    assert len(messages) == 1 and len(messages[0]) == 500
    assert messages[0].startswith("publish-due failed (ValueError)")
    assert messages[0].endswith(url)
    monkeypatch.delenv("GITHUB_RUN_ID")
    assert github_run_url() is None


def test_failed_notifier_is_swallowed(caplog):
    def send(_text):
        raise OSError("private send failure")

    report_failure("job", ValueError("original failure"), send, [], None)
    assert "OSError" in caplog.text
    assert "private send failure" not in caplog.text
