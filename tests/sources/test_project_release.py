import json
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from unittest.mock import Mock

import httpx
import pytest

from postpilot.core.tiktok import TikTokSelection
from postpilot.notify.telegram import Notifier, TelegramApi
from postpilot.projects import Project, Projects
from postpilot.sources.project_release import (
    _notice,
    _releases,
    read_manifest,
    scan_projects,
)
from postpilot.sources.release_assets import (
    _LIMITS,
    DownloadFailed,
    ProjectRelease,
    Rejected,
)
from postpilot.video.label import AI_IMAGE_CAPTION_LABEL, AI_VIDEO_CAPTION_LABEL
from postpilot.video.tiktok import CreatorInfo

NOW = datetime(2026, 11, 27, 8, tzinfo=UTC)
PROJECT = Project("example", "owner/repo", ("reel",), ("linkedin-de", "threads"))
TAG = "reel/sample-1"
VIDEO_LABEL = "\n\n" + AI_VIDEO_CAPTION_LABEL
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


def metadata(files, tag=TAG):
    return [
        {
            "name": name,
            "size": len(content),
            "id": index,
            "updated_at": NOW.isoformat(),
            "browser_download_url": f"https://github.com/owner/repo/releases/download/{tag}/{name}",
        }
        for index, (name, content) in enumerate(files.items(), start=1)
    ]


def scan_page(request, rows):
    assert request.url.params["per_page"] == "100"
    page = int(request.url.params["page"])
    return httpx.Response(
        200,
        json=[{"created_at": NOW.isoformat(), **row} for row in rows]
        if page == 1
        else [],
    )


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
        ({"release_at": "soon"}, "explicit offset"),
        ({"release_at": "2026-11-27T08:00:00"}, "explicit offset"),
        ({"release_at": "2026-11-27T08:00:00+01:99"}, "explicit offset"),
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
        ("post_threads.txt", b"x" * 501, "exceeds 500 characters"),
        ("post_threads.txt", b"\xff", "UTF-8"),
        ("post_threads.txt", b"", "non-empty"),
    ],
)
def test_reel_rejects_invalid_content(name, content, error):
    with pytest.raises(Rejected, match=error):
        load(release_files() | {name: content})


@pytest.mark.parametrize("duration", [2.99, 90.01, float("nan")])
def test_reel_duration_limit(duration):
    with pytest.raises(Rejected, match="video.mp4 must be 3-90 seconds"):
        load(release_files(), duration=duration)


@pytest.mark.parametrize("error", [ValueError, OSError])
def test_reel_probe_error(error):
    def probe(_):
        raise error("not a video")

    files = release_files()
    with httpx.Client(transport=httpx.MockTransport(download(files))) as client:
        release = ProjectRelease(client, PROJECT.repo, TAG, assets=metadata(files))
        with pytest.raises(Rejected, match="could not probe video.mp4 duration"):
            read_manifest(release, PROJECT, NOW, "fingerprint", probe)


def test_project_text_is_verbatim(monkeypatch):
    from postpilot.ai import client, humanizer

    forbidden = Mock(side_effect=AssertionError("Project text must remain verbatim"))
    monkeypatch.setattr(client.anthropic, "Anthropic", forbidden)
    monkeypatch.setattr(humanizer, "humanize_pair", forbidden)
    files = release_files(MANIFEST | {"channels": ["threads"]}) | {
        "post_de.txt": "  Grüße\r\n".encode(),
        "post_threads.txt": b" Threads!\n",
    }
    reel, cover = load(files)
    assert reel.post_de == "  Grüße\r\n"
    assert reel.post_threads == " Threads!\n"
    assert reel.channels == ("threads",)
    assert reel.approval == "tap"
    assert reel.assets == {
        name: {"size": len(data), "sha256": sha256(data).hexdigest()}
        for name, data in files.items()
        if name != "manifest.json"
    }
    assert cover == PNG
    forbidden.assert_not_called()


@pytest.mark.parametrize("filename", ["post_threads.txt", "post_de.txt"])
def test_reel_threads_label_counts_toward_limit(filename):
    raw = "x" * (500 - len(VIDEO_LABEL))
    files = release_files() | {filename: raw.encode()}
    reel, _ = load(files)
    assert reel.post_threads == raw
    assert reel.threads_text == raw + VIDEO_LABEL

    files[filename] += b"x"
    if filename == "post_threads.txt":
        with pytest.raises(
            Rejected,
            match="post_threads.txt exceeds 500 characters including the AI video label",
        ):
            load(files)
    else:
        reel, _ = load(files)
        assert reel.post_threads is None
        assert reel.threads_text is None
        assert f"{len(raw) + 1 + len(VIDEO_LABEL)} chars with AI label" in _notice(
            PROJECT, reel, "new"
        )


@pytest.mark.parametrize("filename", ["post_threads.txt", "post_de.txt"])
def test_non_ai_reel_threads_uses_full_limit(filename):
    files = release_files(MANIFEST | {"ai": False}) | {filename: b"x" * 500}
    reel, _ = load(files)
    assert reel.threads_text == "x" * 500


@pytest.mark.parametrize(
    ("allowlist", "selected", "limit", "platform"),
    [
        (("threads",), None, 3000, "LinkedIn"),
        (("instagram", "threads"), ["threads"], 3000, "LinkedIn"),
        (("instagram", "threads"), None, 2200, "Instagram"),
        (("tiktok", "threads"), ["tiktok"], 2200, "TikTok"),
    ],
)
def test_reel_caption_limit_uses_effective_channels(
    allowlist, selected, limit, platform
):
    project = replace(PROJECT, channels=allowlist)
    manifest = MANIFEST if selected is None else MANIFEST | {"channels": selected}
    raw = b"x" * (limit - len(VIDEO_LABEL))
    files = release_files(manifest) | {"post_de.txt": raw}
    reel, _ = load(files, project)
    assert reel.post_de.encode() == raw
    files["post_de.txt"] += b"x"
    with pytest.raises(Rejected, match=f"{limit}-character {platform} caption limit"):
        load(files, project)


@pytest.fixture
def reel_downloads():
    files = release_files()
    assets = metadata(files)
    requested = []
    redirects = {}

    def read(project=PROJECT):
        content_by_url = {
            asset["browser_download_url"]: files[asset["name"]] for asset in assets
        }
        content_by_url.update(
            {target: content_by_url[url] for url, target in redirects.items()}
        )

        def respond(request):
            assert "authorization" not in request.headers
            url = str(request.url)
            requested.append(url)
            if url in redirects:
                return httpx.Response(302, headers={"Location": redirects[url]})
            return httpx.Response(200, content=content_by_url[url])

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            release = ProjectRelease(client, project.repo, TAG, assets=assets)
            return read_manifest(release, project, NOW, "fingerprint", lambda _: 12)

    return files, assets, requested, redirects, read


@pytest.mark.parametrize(
    "name",
    ["manifest.json", "video.mp4", "cover.png", "post_de.txt", "post_threads.txt"],
)
def test_oversized_metadata_rejects_before_asset_downloads(name, reel_downloads):
    files, assets, requested, _, read = reel_downloads
    if name not in files:
        files[name] = b"Threads"
        assets[:] = metadata(files)
    limit = _LIMITS[name]
    next(asset for asset in assets if asset["name"] == name)["size"] = limit + 1

    with pytest.raises(Rejected, match=rf"{name} exceeds {limit} bytes"):
        read()
    expected = [] if name == "manifest.json" else [assets[0]["browser_download_url"]]
    assert requested == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/owner/repo/releases/download/asset",
        "https://github.com.evil.example/owner/repo/releases/download/asset",
        "https://github.com/other/repo/releases/download/asset",
        "https://github.com/owner/repo/releases/tag/asset",
        "https://github.com/owner/repo/releases/download/%2e%2e/asset",
        "https://objects.githubusercontent.com:8443/asset",
        "https://release-assets.githubusercontent.com/asset",
    ],
)
def test_disallowed_asset_url_rejects_before_reel_downloads(url, reel_downloads):
    _, assets, requested, _, read = reel_downloads
    next(asset for asset in assets if asset["name"] == "video.mp4")[
        "browser_download_url"
    ] = url

    with pytest.raises(Rejected, match=r"video\.mp4 has a disallowed download URL"):
        read()
    assert requested == [assets[0]["browser_download_url"]]


def test_uses_asset_url_and_follows_github_cdn_redirect(reel_downloads):
    files, assets, requested, redirects, read = reel_downloads
    object_url = "https://objects.githubusercontent.com/opaque?asset=cover"
    cdn_url = "https://release-assets.githubusercontent.com/opaque?asset=video"
    next(asset for asset in assets if asset["name"] == "cover.png")[
        "browser_download_url"
    ] = object_url
    video_url = next(asset for asset in assets if asset["name"] == "video.mp4")[
        "browser_download_url"
    ]
    redirects[video_url] = cdn_url

    reel, cover = read()
    assert requested[-3:] == [object_url, video_url, cdn_url]
    assert cover == files["cover.png"]
    assert reel.assets["video.mp4"]["sha256"] == sha256(files["video.mp4"]).hexdigest()


def test_disallowed_download_redirect_is_not_followed(reel_downloads):
    _, assets, requested, redirects, read = reel_downloads
    caption_url = next(asset for asset in assets if asset["name"] == "post_de.txt")[
        "browser_download_url"
    ]
    redirects[caption_url] = "https://other.example/asset"

    with pytest.raises(
        Rejected, match=r"post_de\.txt has a disallowed download redirect"
    ):
        read()
    assert requested == [assets[0]["browser_download_url"], caption_url]


@pytest.mark.parametrize("name", ["video.mp4", "cover.png"])
def test_oversized_asset_body(name, monkeypatch, reel_downloads):
    files, assets, requested, _, read = reel_downloads
    monkeypatch.setitem(_LIMITS, name, 10)
    asset = next(asset for asset in assets if asset["name"] == name)
    asset["size"] = 10
    files[name] = b"x" * 11

    with pytest.raises(Rejected, match=rf"{name} exceeds 10 bytes"):
        read()
    assert requested[-1] == asset["browser_download_url"]


@pytest.mark.parametrize(
    ("raw_title", "title", "notice"),
    [
        (b"  Override \n", "Override", "YouTube title: Override"),
        (b"a" * 101, "", "YouTube will use the default title."),
    ],
)
def test_youtube_title_asset(raw_title, title, notice):
    project = replace(PROJECT, channels=(*PROJECT.channels, "youtube"))
    reel, _ = load(release_files() | {"title_de.txt": raw_title}, project)
    assert reel.title_de == title
    assert reel.assets["title_de.txt"] == {
        "sha256": sha256(raw_title).hexdigest(),
        "size": len(raw_title),
    }
    assert notice in _notice(project, reel, "new")


def test_oversized_youtube_title_asset_keeps_reel_intake(reel_downloads):
    files, assets, requested, _, read = reel_downloads
    name = "title_de.txt"
    files[name] = b"large title"
    assets[:] = metadata(files)
    title_asset = next(asset for asset in assets if asset["name"] == name)
    title_asset["size"] = _LIMITS[name] + 1
    project = replace(PROJECT, channels=(*PROJECT.channels, "youtube"))

    reel, _ = read(project)
    assert reel.title_de == ""
    assert name not in reel.assets
    assert title_asset["browser_download_url"] not in requested
    assert "YouTube will use the default title." in _notice(project, reel, "new")


def test_missing_release_lookup_raises_download_failed():
    url = f"https://api.github.com/repos/{PROJECT.repo}/releases/tags/reel%2Fsample-1"

    def respond(request):
        assert str(request.url) == url
        return httpx.Response(404)

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        pytest.raises(DownloadFailed, match=f"could not read release {TAG}"),
    ):
        ProjectRelease(client, PROJECT.repo, TAG)


def test_threads_skip_or_use_explicit_text():
    project = replace(PROJECT, channels=(*PROJECT.channels, "instagram", "youtube"))
    files = release_files(MANIFEST | {"ai": False}) | {"post_de.txt": b"x" * 2200}
    reel, _ = load(files, project)
    assert reel.threads_text is None
    assert "Threads skipped: post_de.txt is 2200 chars (max 500)" in _notice(
        project, reel, "new"
    )
    reel, _ = load(files | {"post_threads.txt": b"b" * 500}, project)
    assert reel.threads_text == "b" * 500


@pytest.mark.parametrize(("stop", "pages"), [("age", 2), ("empty", 2), ("cap", 10)])
def test_release_scan_pagination(stop, pages, caplog):
    requested = []
    cutoff = NOW - timedelta(days=61)

    def release(tag, created):
        return {
            "tag_name": tag,
            "created_at": created.isoformat(),
            "assets": metadata(release_files(), tag),
        }

    def http(request):
        page = len(requested) + 1
        requested.append(page)
        assert dict(request.url.params) == {"per_page": "100", "page": str(page)}
        assert request.headers["Authorization"] == "Bearer test-token"
        if page == 2 and stop == "empty":
            return httpx.Response(200, json=[])
        rows = [release(f"reel/item-{page}", cutoff)]
        if page == 2 and stop == "age":
            rows.extend(
                [
                    release("unrelated/old", cutoff - timedelta(seconds=1)),
                    release("reel/after-cutoff", cutoff - timedelta(seconds=2)),
                ]
            )
        return httpx.Response(200, json=rows)

    with httpx.Client(transport=httpx.MockTransport(http)) as client:
        releases = list(_releases(PROJECT, client, "test-token", NOW))
    assert requested == list(range(1, pages + 1))
    count = 1 if stop == "empty" else pages
    assert [release.tag for release in releases] == [
        f"reel/item-{page}" for page in range(1, count + 1)
    ]
    assert ("10-page cap" in caplog.text) == (stop == "cap")


def test_scan_filters_and_isolates_projects():
    files = release_files()
    drafts = [
        {"tag_name": TAG, "draft": True, "assets": metadata(files)},
        {"tag_name": TAG, "prerelease": True, "assets": metadata(files)},
        {
            "tag_name": "reel/legacy",
            "assets": metadata(
                {
                    "reel_de.mp4": b"video",
                    "cover_de.png": PNG,
                    "post_de.txt": b"Legacy caption",
                },
                "reel/legacy",
            ),
        },
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
        return scan_page(
            request, [*drafts, {"tag_name": TAG, "assets": metadata(files)}]
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
    assert len(requests) == 6
    assert all(
        request.headers["Authorization"] == "Bearer test-token"
        for request in requests[:4]
    )
    assert all("Authorization" not in request.headers for request in requests[4:])
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
            return scan_page(request, [{"tag_name": TAG, "assets": metadata(files)}])
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


@pytest.mark.parametrize(
    ("already_labelled", "status"), [(False, "new"), (True, "replaced")]
)
@pytest.mark.parametrize("approval", ["auto", "tap"])
@pytest.mark.parametrize("mode", ["draft", "direct"])
@pytest.mark.parametrize("target", [True, False])
def test_scan_sends_notice_or_preview(
    monkeypatch, approval, mode, target, already_labelled, status
):
    from uuid import uuid4

    files = release_files()
    if already_labelled:
        files["post_threads.txt"] = b"  Exact text.\n" + VIDEO_LABEL.encode()
    project = replace(
        PROJECT,
        approval=approval,
        channels=(
            *PROJECT.channels,
            *(("tiktok",) if target else ()),
        ),
    )
    creator = Mock(
        return_value=CreatorInfo(["SELF_ONLY"], False, False, False, 90, "Creator")
    )
    monkeypatch.setattr(
        "postpilot.store.tiktok_settings.TikTokSettingsStore.get_partial",
        lambda *_: TikTokSelection(),
    )

    def http(request):
        if request.url.host == "api.github.com":
            return scan_page(request, [{"tag_name": TAG, "assets": metadata(files)}])
        return download(files)(request)

    import postpilot.sources.project_release as source

    actual = source.read_manifest
    monkeypatch.setattr(
        source, "read_manifest", lambda *args: actual(*args, probe=lambda _: 9)
    )
    store = Mock()
    store.conn.transaction.side_effect = nullcontext
    store.seen.return_value = False
    store.save_item.return_value = (status, None)
    api = Mock(spec=TelegramApi)
    api.send_message.return_value = 17
    store.preview_item_id.return_value = uuid4()
    with httpx.Client(transport=httpx.MockTransport(http)) as client:
        assert scan_projects(
            Projects((project,)),
            client,
            store,
            Notifier(api, 7),
            NOW,
            tiktok_creator=creator if mode == "direct" else None,
        )
    assert store.save_item.call_args.args[1].approval == approval
    message = api.send_message.call_args.args
    assert f"Fri 27.11 09:00 Berlin · {status}" in message[1]
    assert "AI label: yes" in message[1]
    assert f"Threads\n  Exact text.\n{VIDEO_LABEL}" in message[1]
    assert message[1].count(AI_VIDEO_CAPTION_LABEL) == 1
    has_row = mode == "direct" and target
    assert ("TikTok account: Creator" in message[1]) == has_row
    assert creator.call_count == int(has_row)
    if has_row:
        buttons = message[2]["inline_keyboard"]
        assert buttons[-2] == [
            {
                "text": "Only me",
                "callback_data": f"ttp:me:{store.preview_item_id.return_value}",
            }
        ]
        assert len(buttons) == (2 if approval == "auto" else 3)
    if approval == "auto":
        assert len(message) == (3 if has_row else 2)
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


@pytest.mark.parametrize("failure", ["404", "503", "timeout"])
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
            return scan_page(request, [{"tag_name": TAG, "assets": metadata(files)}])
        assert not in_transaction
        if fail_download and request.url.path.endswith("/video.mp4"):
            if failure == "timeout":
                raise httpx.ReadTimeout("asset unavailable", request=request)
            return httpx.Response(int(failure))
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
    store.save_item.side_effect = save
    notify = Mock()
    projects = Projects((PROJECT,))
    with httpx.Client(transport=httpx.MockTransport(http)) as client:
        assert not scan_projects(projects, client, store, notify, NOW)
        store.save_item.assert_not_called()
        store.record_rejection.assert_not_called()
        assert not notify.mock_calls
        assert "could not download asset video.mp4" in caplog.text
        fail_download = False
        assert scan_projects(projects, client, store, notify, NOW)

    assert store.seen.call_args_list[0] == store.seen.call_args_list[1]
    store.save_item.assert_called_once()
    notify.send_project_preview.assert_called_once()
    store.record_rejection.assert_not_called()


POST_TAG = "post/sample-1"
POST_PROJECT = replace(
    PROJECT, kinds=("reel", "post"), channels=("linkedin-de", "threads", "instagram")
)


def post_files(**manifest_fields):
    return {
        "manifest.json": json.dumps(
            MANIFEST | {"kind": "post"} | manifest_fields
        ).encode(),
        "post_de.txt": b"  Exact [text].\n",
    }


def load_post(files, project=POST_PROJECT):
    with httpx.Client(transport=httpx.MockTransport(download(files))) as client:
        return read_manifest(
            ProjectRelease(
                client, project.repo, POST_TAG, assets=metadata(files, POST_TAG)
            ),
            project,
            NOW,
            "fingerprint",
            Mock(side_effect=AssertionError("a post has no video")),
        )


@pytest.mark.parametrize(
    ("text", "explicit_thread", "expected"),
    [
        (b" " + b"x" * 498 + b"\n", None, " " + "x" * 498 + "\n"),
        (b"x" * 501, None, None),
        (b"x" * 3000, b"  A thread.\n", "  A thread.\n"),
    ],
)
def test_post_manifest_preserves_text_and_applies_threads_fallback(
    text, explicit_thread, expected
):
    from hashlib import sha256

    files = post_files() | {"post_de.txt": text}
    if explicit_thread is not None:
        files["post_threads.txt"] = explicit_thread
    item, cover = load_post(files)
    assert item.kind == "post"
    assert item.ai
    assert item.channels == ("linkedin-de", "threads")
    assert item.post_de == text.decode()
    assert item.post_threads == expected
    assert item.threads_skipped == (expected is None)
    assert item.duration is item.title_de is cover is None
    assert item.assets == {
        name: {"sha256": sha256(data).hexdigest(), "size": len(data)}
        for name, data in files.items()
        if name != "manifest.json"
    }


@pytest.mark.parametrize(
    ("fields", "project", "error"),
    [
        ({}, PROJECT, "kind post is not allowed"),
        ({"channels": ["instagram"]}, POST_PROJECT, "not supported for post"),
        ({"channels": ["youtube"]}, POST_PROJECT, "not supported for post"),
        (
            {"channels": ["threads"]},
            replace(POST_PROJECT, channels=("linkedin-de",)),
            "outside the project's allowlist",
        ),
        (
            {},
            replace(POST_PROJECT, channels=("instagram",)),
            "no allowed channels for post",
        ),
    ],
)
def test_post_rejects_disallowed_kind_or_targets(fields, project, error):
    with pytest.raises(Rejected, match=error):
        load_post(post_files(**fields), project)


@pytest.mark.parametrize(
    ("name", "data", "error"),
    [
        ("post_de.txt", None, "missing post_de.txt"),
        ("post_de.txt", b" \n", "non-empty"),
        ("post_de.txt", b"\xff", "UTF-8"),
        ("post_de.txt", b"x" * 3001, "exceeds 3000 characters"),
        ("post_threads.txt", b"", "non-empty"),
        ("post_threads.txt", b"x" * 501, "exceeds 500 characters"),
    ],
)
def test_post_rejects_invalid_text(name, data, error):
    files = post_files()
    if data is None:
        del files[name]
    else:
        files[name] = data
    with pytest.raises(Rejected, match=error):
        load_post(files)


def scan_post(files, project, store, notify):
    def http(request):
        if request.url.host == "api.github.com":
            return scan_page(
                request, [{"tag_name": POST_TAG, "assets": metadata(files, POST_TAG)}]
            )
        return download(files)(request)

    with httpx.Client(transport=httpx.MockTransport(http)) as client:
        return scan_projects(Projects((project,)), client, store, notify, NOW)


@pytest.mark.parametrize("approval", ["auto", "tap"])
def test_post_scan_sends_text_only_notice_or_split_preview(approval):
    post_de = "😀" * 2300
    files = post_files() | {"post_de.txt": post_de.encode()}
    store = Mock()
    store.conn.transaction.side_effect = nullcontext
    store.seen.return_value = False
    store.save_item.return_value = ("new", None)
    api = Mock(spec=TelegramApi)
    api.send_message.side_effect = [17, 18]
    project = replace(POST_PROJECT, approval=approval)
    assert scan_post(files, project, store, Notifier(api, 7))
    stored = store.save_item.call_args.args[1]
    assert stored.kind == "post" and stored.approval == approval
    assert stored.post_threads is None
    api.send_photo.assert_not_called()
    store.record_rejection.assert_not_called()
    messages = [call.args for call in api.send_message.call_args_list]
    heading = messages[0][1]
    assert "Threads skipped: post_de.txt is 2300 chars" in heading
    assert "AI label: no" in heading
    assert "YouTube" not in heading
    if approval == "auto":
        assert len(messages) == 1 and len(messages[0]) == 2
        store.set_preview_message.assert_not_called()
    else:
        assert len(messages) == 2 and messages[0][2] is None
        assert "".join(message[1] for message in messages).endswith(post_de)
        buttons = messages[1][2]["inline_keyboard"][0]
        item_id = store.preview_item_id.return_value
        assert [button["callback_data"] for button in buttons] == [
            f"papprove:{item_id}",
            f"preject:{item_id}",
        ]
        store.set_preview_message.assert_called_once_with(item_id, 18)


@pytest.mark.parametrize(
    ("name", "photo"), [("image.png", PNG), ("image.jpg", b"\xff\xd8\xff")]
)
@pytest.mark.parametrize("ai", [True, False])
def test_image_post_intake_and_preview(name, photo, ai):
    files = post_files(ai=ai) | {name: photo}
    store = Mock()
    store.conn.transaction.side_effect = nullcontext
    store.seen.return_value = False
    store.save_item.return_value = ("new", None)
    api = Mock(spec=TelegramApi)
    assert scan_post(files, POST_PROJECT, store, Notifier(api, 7))
    stored = store.save_item.call_args.args[1]
    assert stored.image_name == name
    assert stored.assets[name] == {
        "sha256": sha256(photo).hexdigest(),
        "size": len(photo),
    }
    assert stored.post_de == files["post_de.txt"].decode()
    assert stored.post_threads == stored.post_de
    api.send_photo.assert_called_once_with(7, photo)
    message = api.send_message.call_args.args[1]
    assert "Image: yes" in message
    assert f"AI label: {'yes' if ai else 'no'}" in message
    suffix = "\n\n" + AI_IMAGE_CAPTION_LABEL if ai else ""
    assert f"LinkedIn DE\n{stored.post_de}{suffix}" in message
    assert message.endswith(f"Threads\n{stored.post_threads}{suffix}")


@pytest.mark.parametrize(
    ("images", "reason"),
    [
        ({"image.png": PNG, "image.jpg": b"\xff\xd8\xff"}, "only one"),
        ({"image.png": PNG + b"x" * 8_000_000}, "exceeds 8000000 bytes"),
        ({"image.png": b"\xff\xd8\xff"}, "invalid image signature"),
        ({"image.jpg": PNG}, "invalid image signature"),
    ],
)
def test_image_post_rejects_invalid_assets(images, reason):
    with pytest.raises(Rejected, match=reason):
        load_post(post_files() | images)


@pytest.mark.parametrize(
    ("name", "limit"), [("post_de.txt", 3000), ("post_threads.txt", 500)]
)
def test_image_label_counts_toward_text_limit(name, limit):
    limit_with_label = limit - len(AI_IMAGE_CAPTION_LABEL) - 2
    files = post_files() | {"image.png": PNG, name: b"x" * limit_with_label}
    load_post(files)
    files[name] += b"x"
    with pytest.raises(
        Rejected,
        match=rf"{name} exceeds {limit} characters including the AI image label",
    ):
        load_post(files)
    load_post(files | post_files(ai=False) | {name: files[name]})


def test_image_label_counts_toward_threads_fallback_limit():
    limit = 500 - len(AI_IMAGE_CAPTION_LABEL) - 2
    files = post_files() | {"image.png": PNG, "post_de.txt": b"x" * limit}
    assert load_post(files)[0].post_threads == "x" * limit
    files["post_de.txt"] += b"x"
    assert load_post(files)[0].post_threads is None


def test_post_preview_shows_only_selected_channel():
    files = post_files(channels=["threads"]) | {"post_threads.txt": b"Thread only.\n"}
    store = Mock()
    store.conn.transaction.side_effect = nullcontext
    store.seen.return_value = False
    store.save_item.return_value = ("new", None)
    api = Mock(spec=TelegramApi)
    assert scan_post(files, POST_PROJECT, store, Notifier(api, 7))
    message = api.send_message.call_args.args[1]
    assert "\n\nThreads\nThread only.\n" in message
    assert "LinkedIn DE" not in message
    assert "Exact [text]" not in message


def test_auto_image_post_sends_notice_with_photo_and_no_approval_buttons():
    store = Mock()
    store.conn.transaction.side_effect = nullcontext
    store.seen.return_value = False
    store.save_item.return_value = ("new", None)
    api = Mock(spec=TelegramApi)
    project = replace(POST_PROJECT, approval="auto")
    assert scan_post(
        post_files() | {"image.png": PNG}, project, store, Notifier(api, 7)
    )
    api.send_photo.assert_called_once_with(7, PNG)
    chat_id, notice = api.send_message.call_args.args
    assert chat_id == 7 and "Image: yes" in notice
    store.set_preview_message.assert_not_called()
