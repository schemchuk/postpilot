import json
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock

import httpx
import pytest

from postpilot.notify.telegram import Notifier, TelegramApi
from postpilot.projects import Project, Projects
from postpilot.sources.factory import ProjectRelease, Rejected
from postpilot.sources.project_release import read_manifest, scan_projects

NOW = datetime(2026, 11, 27, 8, tzinfo=UTC)
PROJECT = Project("example", "owner/repo", ("reel",), ("linkedin-de", "threads"))
TAG = "reel/sample-1"
PNG = b"\x89PNG\r\n\x1a\n"
MANIFEST = {
    "v": 2,
    "kind": "reel",
    "id": "sample-1",
    "release_at": NOW.isoformat(),
    "ai": True,
}


def release_files(manifest=None):
    return {
        "manifest.json": json.dumps(
            MANIFEST if manifest is None else manifest
        ).encode(),
        "video.mp4": b"video",
        "cover.png": PNG,
        "post_de.txt": b"  Exact text.\n",
    }


def metadata(files):
    return [
        {
            "name": name,
            "size": len(content),
            "id": index,
            "updated_at": NOW.isoformat(),
            "browser_download_url": f"https://github.com/owner/repo/releases/download/{TAG}/{name}",
        }
        for index, (name, content) in enumerate(files.items(), start=1)
    ]


def download(files):
    def handle(request):
        assert request.headers.get("Authorization") is None
        return httpx.Response(200, content=files[request.url.path.split("/")[-1]])

    return handle


def load(files, project=PROJECT, duration=12):
    with httpx.Client(transport=httpx.MockTransport(download(files))) as client:
        return read_manifest(
            ProjectRelease(client, project.repo, TAG, assets=metadata(files)),
            project,
            NOW,
            "fingerprint",
            lambda _: duration,
        )


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"extra": 0}, "unknown"),
        ({"v": True}, "integer 2"),
        ({"v": 1}, "integer 2"),
        ({"v": 2.0}, "integer 2"),
        ({"kind": "post"}, "tag prefix"),
        ({"id": "other"}, "tag suffix"),
        ({"id": "Bad--slug"}, "lowercase slug"),
        ({"id": "a" * 81}, "at most 80"),
        ({"ai": 1}, "boolean"),
        ({"release_at": "2026-11-27T08:00:00"}, "explicit offset"),
        ({"release_at": "2026-11-27T06:59:59Z"}, "within 1 hour"),
        ({"release_at": "2027-02-01T08:00:00Z"}, "60 days"),
        ({"channels": []}, "non-empty list"),
        ({"channels": None}, "non-empty list"),
        ({"channels": ["unknown"]}, "unknown channel"),
        ({"channels": ["linkedin-ua"]}, "paused"),
        ({"channels": ["youtube"]}, "outside the project's allowlist"),
        ({"channels": ["threads", "threads"]}, "duplicate channel"),
    ],
)
def test_manifest_rejects_invalid_fields(fields, error):
    with pytest.raises(Rejected, match=error):
        load(release_files(MANIFEST | fields))


@pytest.mark.parametrize("key", MANIFEST.keys())
def test_manifest_requires_each_field(key):
    with pytest.raises(Rejected, match=f"missing:.*{key}"):
        load(release_files({k: v for k, v in MANIFEST.items() if k != key}))


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (b"{", "valid JSON"),
        (b"[]", "JSON object"),
        (b"\xff", "UTF-8"),
        (b'{"v":2,"v":2}', "duplicate keys"),
        (b" " * 16_001, "exceeds 16000 bytes"),
    ],
)
def test_manifest_rejects_invalid_encoding_and_shape(raw, error):
    with pytest.raises(Rejected, match=error):
        load(release_files() | {"manifest.json": raw})


def test_manifest_cannot_select_unallowed_kind():
    with pytest.raises(Rejected, match="kind reel is not allowed for project example"):
        load(release_files(), replace(PROJECT, kinds=("post",)))


@pytest.mark.parametrize("name", release_files().keys())
def test_manifest_requires_assets(name):
    files = release_files()
    del files[name]
    with pytest.raises(Rejected, match=f"missing {name}"):
        load(files)


@pytest.mark.parametrize(
    ("name", "content", "error"),
    [
        ("cover.png", b"image", "PNG signature"),
        ("post_de.txt", b"\xff", "UTF-8"),
        ("post_de.txt", b"  \n ", "non-empty"),
        ("post_de.txt", b"a\0b", "without NUL"),
        ("post_de.txt", b"x" * 3001, "exceeds 3000 characters"),
        ("post_de.txt", b"x" * 2200, "Instagram caption limit"),
        ("post_threads.txt", b"x" * 501, "exceeds 500 characters"),
        ("post_threads.txt", b"\xff", "UTF-8"),
    ],
)
def test_reel_rejects_invalid_content(name, content, error):
    with pytest.raises(Rejected, match=error):
        load(release_files() | {name: content})


@pytest.mark.parametrize("duration", [2.99, 90.01, float("nan")])
def test_reel_duration_limit(duration):
    with pytest.raises(Rejected, match="video.mp4 must be 3-90 seconds"):
        load(release_files(), duration=duration)


def test_reel_probe_error():
    def probe(_):
        raise ValueError("not a video")

    files = release_files()
    with httpx.Client(transport=httpx.MockTransport(download(files))) as client:
        release = ProjectRelease(client, PROJECT.repo, TAG, assets=metadata(files))
        with pytest.raises(Rejected, match="could not probe video.mp4 duration"):
            read_manifest(release, PROJECT, NOW, "fingerprint", probe)


def test_project_text_is_verbatim():
    files = release_files(MANIFEST | {"channels": ["threads"]})
    reel, cover = load(files | {"post_threads.txt": b" Threads!\n"})
    assert reel.post_de == "  Exact text.\n"
    assert reel.post_threads == " Threads!\n"
    assert reel.channels == ("threads",)
    assert reel.approval == "tap"
    assert set(reel.assets) == set(files) - {"manifest.json"} | {"post_threads.txt"}
    assert cover == PNG


def test_scan_filters_and_isolates_projects():
    files = release_files()
    drafts = [
        {"tag_name": TAG, "draft": True, "assets": metadata(files)},
        {"tag_name": TAG, "prerelease": True, "assets": metadata(files)},
        {"tag_name": "reel/legacy", "assets": []},
        {"tag_name": "post/example", "assets": metadata(files)},
    ]
    store = Mock()
    store.conn.transaction.side_effect = nullcontext
    store.seen.return_value = True
    notify = Mock()
    requests = []

    def http(request):
        requests.append(request)
        if request.url.path == "/repos/owner/failed/releases":
            return httpx.Response(503)
        if request.url.path == "/repos/owner/bad-json/releases":
            return httpx.Response(200, content=b"invalid")
        assert request.url.path == "/repos/owner/repo/releases"
        assert dict(request.url.params) == {"per_page": "30"}
        return httpx.Response(
            200, json=[*drafts, {"tag_name": TAG, "assets": metadata(files)}]
        )

    projects = Projects(
        (
            replace(PROJECT, key="failed", repo="owner/failed"),
            replace(PROJECT, key="bad-json", repo="owner/bad-json"),
            replace(PROJECT, key="disabled", repo="owner/disabled", enabled=False),
            PROJECT,
        )
    )
    with httpx.Client(transport=httpx.MockTransport(http)) as client:
        assert not scan_projects(
            projects, client, store, notify, NOW, token="test-token"
        )
        assert scan_projects(projects, client, store, notify, NOW, only=PROJECT.key)
    assert len(requests) == 4
    assert all(
        request.headers["Authorization"] == "Bearer test-token"
        for request in requests[:3]
    )
    assert "Authorization" not in requests[-1].headers
    assert store.seen.call_count == 2
    assert store.seen.call_args_list[0] == store.seen.call_args_list[1]
    assert store.seen.call_args.args[:2] == (PROJECT.key, TAG)
    assert not notify.mock_calls


def test_scan_rejection_fingerprint_is_saved_and_notified_once():
    files = release_files() | {"manifest.json": b"{}"}
    seen = set()
    downloads = []

    def http(request):
        if request.url.host == "api.github.com":
            return httpx.Response(
                200, json=[{"tag_name": TAG, "assets": metadata(files)}]
            )
        downloads.append(request.url.path)
        return download(files)(request)

    def reject(project, tag, fingerprint, reason):
        seen.add((project, tag, fingerprint))
        return True

    store = Mock()
    store.conn.transaction.side_effect = nullcontext
    store.seen.side_effect = lambda *key: key in seen
    store.record_rejection.side_effect = reject
    notify = Mock()
    with httpx.Client(transport=httpx.MockTransport(http)) as client:
        for _ in range(2):
            assert scan_projects(Projects((PROJECT,)), client, store, notify, NOW)
    assert len(downloads) == 1
    store.record_rejection.assert_called_once()
    notify.send_alert.assert_called_once()
    assert notify.send_alert.call_args.args[0].startswith(
        f"Project {PROJECT.key} {TAG} rejected:"
    )


@pytest.mark.parametrize("approval", ["auto", "tap"])
def test_scan_sends_notice_or_preview(monkeypatch, approval):
    files = release_files()
    project = replace(PROJECT, approval=approval)

    def http(request):
        if request.url.host == "api.github.com":
            return httpx.Response(
                200, json=[{"tag_name": TAG, "assets": metadata(files)}]
            )
        return download(files)(request)

    import postpilot.sources.project_release as source

    actual = source.read_manifest
    monkeypatch.setattr(
        source, "read_manifest", lambda *args: actual(*args, probe=lambda _: 9)
    )
    store = Mock()
    store.conn.transaction.side_effect = nullcontext
    store.seen.return_value = False
    store.save_reel.return_value = ("new", None)
    api = Mock(spec=TelegramApi)
    api.send_message.return_value = 17
    with httpx.Client(transport=httpx.MockTransport(http)) as client:
        assert scan_projects(Projects((project,)), client, store, Notifier(api, 7), NOW)
    assert store.save_reel.call_args.args[1].approval == approval
    message = api.send_message.call_args.args
    assert "Fri 27.11 09:00 Berlin" in message[1]
    assert "AI label: yes" in message[1]
    if approval == "auto":
        assert len(message) == 2
        api.send_photo.assert_not_called()
    else:
        api.send_photo.assert_called_once_with(7, PNG)
        assert "  Exact text.\n" in message[1]
        buttons = message[2]["inline_keyboard"][0]
        assert [button["text"] for button in buttons] == ["Approve", "Reject"]
        store.set_preview_message.assert_called_once_with(
            store.preview_item_id.return_value, 17
        )


def test_publishing_reads_and_verifies_v2_asset_names():
    from hashlib import sha256
    from uuid import uuid4

    from postpilot.channels.base import ChannelError
    from postpilot.channels.factory import FactoryAssets
    from postpilot.store.project_items import ProjectItem

    files = release_files()
    downloaded = []

    def http(request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"assets": metadata(files)})
        downloaded.append(request.url.path.split("/")[-1])
        return download(files)(request)

    assets = {
        name: {"size": len(files[name]), "sha256": sha256(files[name]).hexdigest()}
        for name in ("video.mp4", "cover.png")
    }
    item = ProjectItem("sample-1", NOW, True, "Text", "Thread", assets, 10, PROJECT.key)
    store = Mock()
    store.get_by_item.return_value = item
    with httpx.Client(transport=httpx.MockTransport(http)) as client:
        reader = FactoryAssets(store, client, Projects((PROJECT,)))
        item_id = uuid4()
        reader.check_release(item_id)
        assert reader.video(item_id) == files["video.mp4"]
        assert reader.cover(item_id) == PNG
        assert reader.cover_url(item_id).endswith("/cover.png")
        assert downloaded == ["video.mp4", "cover.png"]
        files["video.mp4"] = b"changed"
        with pytest.raises(ChannelError, match="video.mp4 changed since intake"):
            reader.video(uuid4())


@pytest.mark.parametrize("failure", ["503", "timeout"])
def test_scan_retries_failed_download_with_same_fingerprint(
    monkeypatch, caplog, failure
):
    import postpilot.sources.project_release as source

    files = release_files()
    fail_download = True
    in_transaction = False

    @contextmanager
    def transaction():
        nonlocal in_transaction
        assert not in_transaction
        in_transaction = True
        try:
            yield
        finally:
            in_transaction = False

    def http(request):
        if request.url.host == "api.github.com":
            return httpx.Response(
                200, json=[{"tag_name": TAG, "assets": metadata(files)}]
            )
        assert not in_transaction
        if fail_download and request.url.path.endswith("/video.mp4"):
            if failure == "timeout":
                raise httpx.ReadTimeout("asset unavailable", request=request)
            return httpx.Response(503)
        return download(files)(request)

    def probe(_):
        assert not in_transaction
        return 9

    def save(*_):
        assert in_transaction
        return "new", None

    actual = source.read_manifest
    monkeypatch.setattr(
        source, "read_manifest", lambda *args: actual(*args, probe=probe)
    )
    store = Mock()
    store.conn.transaction.side_effect = transaction
    store.seen.return_value = False
    store.save_reel.side_effect = save
    notify = Mock()
    projects = Projects((PROJECT,))
    with httpx.Client(transport=httpx.MockTransport(http)) as client:
        assert not scan_projects(projects, client, store, notify, NOW)
        store.save_reel.assert_not_called()
        store.record_rejection.assert_not_called()
        assert not notify.mock_calls
        assert "could not download asset video.mp4" in caplog.text
        fail_download = False
        assert scan_projects(projects, client, store, notify, NOW)

    assert store.seen.call_args_list[0] == store.seen.call_args_list[1]
    store.save_reel.assert_called_once()
    notify.send_project_preview.assert_called_once()
    store.record_rejection.assert_not_called()
