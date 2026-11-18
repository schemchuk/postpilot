from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_callback_keeps_query_data_in_text():
    site = ROOT / "site"
    html = (site / "callback" / "index.html").read_text()
    assert (site / ".nojekyll").is_file()
    assert "innerHTML" not in html
    assert "http://" not in html
    assert "https://" not in html
    assert '<meta name="referrer" content="no-referrer">' in html
    assert 'http-equiv="Content-Security-Policy"' in html
    assert "default-src 'none'" in html
    assert 'command.textContent = "/renew "' in html
    assert "history.replaceState" in html
    assert "navigator.clipboard.writeText" in html


def test_pages_workflow():
    workflow = ROOT / ".github" / "workflows" / "pages.yml"
    parsed = yaml.load(workflow.read_text(), Loader=yaml.BaseLoader)
    assert parsed["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    assert parsed["on"] == {
        "push": {
            "branches": ["main"],
            "paths": ["site/**", ".github/workflows/pages.yml"],
        },
        "workflow_dispatch": "",
    }
    assert parsed["concurrency"] == {"group": "pages", "cancel-in-progress": "false"}
    job = parsed["jobs"]["deploy"]
    assert "permissions" not in job
    steps = job["steps"]
    assert [step["uses"] for step in steps] == [
        "actions/checkout@v4",
        "actions/configure-pages@v5",
        "actions/upload-pages-artifact@v3",
        "actions/deploy-pages@v4",
    ]
    assert steps[2]["with"] == {"path": "site"}
