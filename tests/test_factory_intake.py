import hashlib
import json
from datetime import UTC, datetime
from unittest.mock import Mock

import httpx
import pytest

from postpilot import cli
from postpilot.config import DEFAULT_FACTORY_REPO, Settings
from postpilot.sources import factory

REPO = DEFAULT_FACTORY_REPO
NOW = datetime(2026, 11, 26, tzinfo=UTC)
EPISODE = "002-medunko"
TAG = f"reel/{EPISODE}"
PAYLOAD = {"v": 1, "episode": EPISODE, "ai": True, "release_at": "2026-12-01T07:00:00Z"}
ASSETS = {
    "reel_de.mp4": b"video",
    "cover_de.png": b"\x89PNG\r\n\x1a\nimage",
    "post_de.txt": "  Grüße\r\n".encode(),
}
URL = f"https://github.com/{REPO}/releases/tag/{TAG}"


def run(
    raw,
    *,
    assets=None,
    status="new",
    duration=3,
    release=200,
    download=200,
    urls=None,
    sizes=None,
    downloads=None,
    redirects=None,
):
    assets = ASSETS if assets is None else assets
    urls = {
        name: f"https://github.com/{REPO}/releases/download/reel%2F{EPISODE}/{name}"
        for name in assets
    } | (urls or {})
    sizes = {name: len(content) for name, content in assets.items()} | (sizes or {})
    redirects = redirects or {}
    save, notice = Mock(return_value=status), Mock()

    def respond(request):
        assert "authorization" not in request.headers
        if request.url.host == "api.github.com":
            assert (
                str(request.url)
                == f"https://api.github.com/repos/{REPO}/releases/tags/reel%2F{EPISODE}"
            )
            return httpx.Response(
                release,
                json={
                    "assets": [
                        {
                            "name": name,
                            "browser_download_url": urls[name],
                            "size": sizes[name],
                        }
                        for name in assets
                    ]
                },
            )
        url = str(request.url)
        if downloads is not None:
            downloads.append(url)
        for name, asset_url in urls.items():
            if url == asset_url and name in redirects:
                return httpx.Response(302, headers={"Location": redirects[name]})
            if url == redirects.get(name, asset_url):
                return httpx.Response(download, content=assets[name])
        pytest.fail(f"Unexpected asset URL: {url}")

    probe = duration if callable(duration) else Mock(return_value=duration)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        code = factory.intake(raw, REPO, client, save, notice, now=NOW, probe=probe)
    notice.assert_called_once()
    return code, notice.call_args.args[0], save


@pytest.mark.parametrize(
    ("raw", "episode", "reason"),
    [
        ("{", "?", "payload must be valid JSON"),
        ("[]", "?", "payload must be a JSON object"),
        ('{"v":1,"v":1}', "?", "payload contains duplicate keys"),
        (json.dumps(PAYLOAD | {"repo": "other/repo"}), EPISODE, "payload keys"),
        (
            json.dumps({k: v for k, v in PAYLOAD.items() if k != "ai"}),
            EPISODE,
            "payload keys",
        ),
        (json.dumps(PAYLOAD | {"v": 2}), EPISODE, "v must be the integer 1"),
        (json.dumps(PAYLOAD | {"v": True}), EPISODE, "v must be the integer 1"),
        (json.dumps(PAYLOAD | {"ai": 1}), EPISODE, "ai must be a boolean"),
        (
            json.dumps(PAYLOAD | {"episode": "002-../E"}),
            "?",
            "episode must be a factory slug",
        ),
        (
            json.dumps(PAYLOAD | {"episode": "002-" + "a" * 77}),
            "?",
            "at most 80 characters",
        ),
        (json.dumps(PAYLOAD | {"release_at": "soon"}), EPISODE, "explicit offset"),
        (
            json.dumps(PAYLOAD | {"release_at": "2026-12-01T07:00"}),
            EPISODE,
            "explicit offset",
        ),
        (
            json.dumps(PAYLOAD | {"release_at": "2026-12-01T07:00+01:99"}),
            EPISODE,
            "explicit offset",
        ),
        (
            json.dumps(PAYLOAD | {"release_at": "2026-11-25T22:59:59Z"}),
            EPISODE,
            "within 1 hour",
        ),
        (
            json.dumps(PAYLOAD | {"release_at": "2027-01-25T00:00:01Z"}),
            EPISODE,
            "60 days ahead",
        ),
    ],
)
def test_bad_payload(raw, episode, reason):
    code, message, save = run(raw)
    assert code == 1
    assert message.startswith(f"Factory reel {episode} rejected: ")
    assert reason in message
    save.assert_not_called()


@pytest.mark.parametrize("missing", tuple(ASSETS))
def test_missing_asset(missing):
    code, message, save = run(
        json.dumps(PAYLOAD), assets={k: v for k, v in ASSETS.items() if k != missing}
    )
    assert code == 1
    assert (
        message
        == f"Factory reel {EPISODE} rejected: release {TAG} is missing {missing}"
    )
    save.assert_not_called()


@pytest.mark.parametrize(
    ("name", "content", "reason"),
    [
        ("cover_de.png", b"bad", "PNG signature"),
        ("post_de.txt", b"\xff", "must be UTF-8"),
        ("post_de.txt", b" \n", "non-empty"),
        ("post_de.txt", b"\0", "without NUL"),
        ("post_de.txt", b"a" * 3001, "3000 characters"),
        ("post_de.txt", b"a" * 2201, "2200-character"),
        ("post_de.txt", b"a" * 2200, "including any AI label"),
        ("post_threads.txt", b"\xff", "must be UTF-8"),
        ("post_threads.txt", b"", "non-empty"),
        ("post_threads.txt", b"a" * 501, "500 characters"),
    ],
)
def test_invalid_asset(name, content, reason):
    code, message, save = run(json.dumps(PAYLOAD), assets=ASSETS | {name: content})
    assert code == 1
    assert name in message and reason in message
    save.assert_not_called()


@pytest.mark.parametrize("name", ["reel_de.mp4", "cover_de.png"])
def test_oversized_asset(name, monkeypatch):
    monkeypatch.setitem(factory._LIMITS, name, 10)
    code, message, save = run(
        json.dumps(PAYLOAD), assets=ASSETS | {name: b"a" * 11}, sizes={name: 10}
    )
    assert code == 1
    assert message.endswith(f"{name} exceeds 10 bytes")
    save.assert_not_called()


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"release": 404}, f"could not read release {TAG}"),
        ({"download": 404}, "could not download asset post_de.txt"),
        (
            {"duration": Mock(side_effect=OSError)},
            "could not probe reel_de.mp4 duration",
        ),
        ({"duration": 2.99}, "reel_de.mp4 must be 3-90 seconds"),
        ({"duration": 90.01}, "reel_de.mp4 must be 3-90 seconds"),
        ({"duration": float("nan")}, "reel_de.mp4 must be 3-90 seconds"),
    ],
)
def test_release_and_duration_errors(options, reason):
    code, message, save = run(json.dumps(PAYLOAD), **options)
    assert code == 1
    assert message == f"Factory reel {EPISODE} rejected: {reason}"
    save.assert_not_called()


@pytest.mark.parametrize("status", ["new", "replaced", "unchanged", "refused"])
def test_notice_and_verbatim_text(status, monkeypatch):
    from postpilot.ai import client, humanizer

    forbidden = Mock(side_effect=AssertionError("Factory intake must not call AI"))
    monkeypatch.setattr(client.anthropic, "Anthropic", forbidden)
    monkeypatch.setattr(humanizer, "humanize_pair", forbidden)
    code, message, save = run(json.dumps(PAYLOAD), status=status, duration=90)
    assert code == int(status == "refused")
    status_line = "refused (already published)" if status == "refused" else status
    assert message == (
        f"Factory reel {EPISODE} · scheduled Tue 01.12 08:00 · LinkedIn DE, Threads, IG, FB, YouTube · AI label: yes\n"
        f"Status: {status_line}\n{URL}"
    )
    reel = save.call_args.args[0]
    assert reel.post_de == reel.post_threads == "  Grüße\r\n"
    assert reel.assets == {
        name: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for name, content in ASSETS.items()
    }
    forbidden.assert_not_called()


def test_threads_skip_or_use_explicit_text():
    raw = json.dumps(PAYLOAD | {"ai": False})
    assets = ASSETS | {"post_de.txt": b"a" * 2200}
    code, message, save = run(raw, assets=assets)
    assert code == 0
    assert message == (
        f"Factory reel {EPISODE} · scheduled Tue 01.12 08:00 · LinkedIn DE, IG, FB, YouTube · AI label: no\n"
        f"Threads skipped: post_de.txt is 2200 chars (max 500), no post_threads.txt\n"
        f"YouTube will use the default title.\nStatus: new\n{URL}"
    )
    assert save.call_args.args[0].post_threads is None
    code, _, save = run(raw, assets=assets | {"post_threads.txt": b"b" * 500})
    assert code == 0
    assert save.call_args.args[0].post_threads == "b" * 500


@pytest.mark.parametrize("explicit", [True, False])
def test_cli_payload_with_no_encryption_key(monkeypatch, tmp_path, explicit):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FACTORY_PAYLOAD", "from-env")
    monkeypatch.setenv("FACTORY_REPO", "owner/factory")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "unused")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    command = Mock(return_value=1)
    monkeypatch.setattr(cli, "_factory_intake", command)
    args = ["factory-intake"]
    if explicit:
        args += ["--payload", "from-flag"]
    assert cli.main(args) == 1
    raw, settings = command.call_args.args
    assert raw == ("from-flag" if explicit else "from-env")
    assert isinstance(settings, Settings)
    assert settings.factory_repo == "owner/factory"


def test_core_publisher_rejects_factory_before_side_effects():
    from uuid import uuid4

    from postpilot.core.models import Item
    from postpilot.core.publisher import Publisher
    from postpilot.core.states import IllegalTransition, ItemState

    item_id = uuid4()
    repo = Mock()
    repo.get_item.return_value = Item(
        item_id, "factory", ItemState.APPROVED, NOW, NOW, None, NOW
    )
    channel = Mock()
    with pytest.raises(IllegalTransition, match="factory publisher"):
        Publisher(repo, [channel]).publish_item(item_id)
    repo.transition.assert_not_called()
    repo.claim_publication.assert_not_called()
    assert channel.mock_calls == []


@pytest.mark.parametrize("name", ["reel_de.mp4", "cover_de.png"])
def test_oversized_metadata_rejects_before_any_download(name):
    downloads = []
    limit = factory._LIMITS[name]
    code, message, save = run(
        json.dumps(PAYLOAD), sizes={name: limit + 1}, downloads=downloads
    )
    assert code == 1
    assert message.endswith(f"{name} exceeds {limit} bytes")
    assert downloads == []
    save.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        f"http://github.com/{REPO}/releases/download/asset",
        f"https://github.com.evil.example/{REPO}/releases/download/asset",
        "https://github.com/other/repo/releases/download/asset",
        f"https://github.com/{REPO}/releases/tag/asset",
        f"https://github.com/{REPO}/releases/download/%2e%2e/asset",
        "https://objects.githubusercontent.com:8443/asset",
        "https://release-assets.githubusercontent.com/asset",
    ],
)
def test_disallowed_asset_url_rejects_before_any_download(url):
    downloads = []
    code, message, save = run(
        json.dumps(PAYLOAD), urls={"reel_de.mp4": url}, downloads=downloads
    )
    assert code == 1
    assert message.endswith("reel_de.mp4 has a disallowed download URL")
    assert downloads == []
    save.assert_not_called()


def test_uses_asset_url_and_follows_github_cdn_redirect():
    object_url = "https://objects.githubusercontent.com/opaque?asset=cover"
    cdn_url = "https://release-assets.githubusercontent.com/opaque?asset=video"
    downloads = []
    code, _, save = run(
        json.dumps(PAYLOAD),
        urls={"cover_de.png": object_url},
        redirects={"reel_de.mp4": cdn_url},
        downloads=downloads,
    )
    assert code == 0
    assert object_url in downloads and cdn_url in downloads
    save.assert_called_once()


def test_disallowed_download_redirect_is_not_followed():
    downloads = []
    target = "https://other.example/asset"
    code, message, save = run(
        json.dumps(PAYLOAD),
        redirects={"post_de.txt": target},
        downloads=downloads,
    )
    assert code == 1
    assert message.endswith("post_de.txt has a disallowed download redirect")
    assert len(downloads) == 1 and target not in downloads
    save.assert_not_called()


@pytest.mark.parametrize(
    "raw_title, expected",
    [(b"  Override \n", "Override"), (b"a" * 101, "")],
)
def test_youtube_title_asset(raw_title, expected):
    code, message, save = run(
        json.dumps(PAYLOAD), assets=ASSETS | {"title_de.txt": raw_title}
    )
    assert code == 0
    assert "IG, FB, YouTube" in message
    assert ("YouTube will use the default title." in message) == (not expected)
    reel = save.call_args.args[0]
    assert reel.title_de == expected
    assert (
        reel.assets["title_de.txt"]["sha256"] == hashlib.sha256(raw_title).hexdigest()
    )


def test_oversized_youtube_title_asset_keeps_factory_intake():
    name = "title_de.txt"
    downloads = []
    code, message, save = run(
        json.dumps(PAYLOAD),
        assets=ASSETS | {name: b"large title"},
        sizes={name: factory._LIMITS[name] + 1},
        downloads=downloads,
    )
    assert code == 0
    assert "YouTube will use the default title." in message
    assert save.call_args.args[0].title_de == ""
    assert not any(url.endswith(name) for url in downloads)
