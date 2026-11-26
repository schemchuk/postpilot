import json
import logging
from contextlib import nullcontext
from io import BytesIO
from unittest.mock import Mock

import httpx
import pytest

from postpilot import cli
from postpilot.config import Settings, TikTokSettings
from postpilot.jobs import tasks
from postpilot.video.tiktok import InboxUpload, TikTokAPIError, TikTokClient, UploadPlan

MB = 1024**2


@pytest.mark.parametrize(
    ("size", "chunk", "count"),
    [
        (1, 1, 1),
        (64 * MB, 64 * MB, 1),
        (64 * MB + 1, 10 * MB, 6),
        (70 * MB, 10 * MB, 7),
    ],
)
def test_upload_plan(size, chunk, count):
    plan = UploadPlan(size)
    assert (plan.chunk_size, plan.total_chunk_count) == (chunk, count)


def test_empty_video_rejected():
    with pytest.raises(ValueError, match="must be non-empty"):
        UploadPlan(0)


def test_put_merges_remainder_into_last_chunk(monkeypatch):
    monkeypatch.setattr("postpilot.video.tiktok._CHUNK_SIZE", 10)
    monkeypatch.setattr("postpilot.video.tiktok._SINGLE_CHUNK_LIMIT", 64)
    requests = []

    def respond(request):
        requests.append((request.headers["content-range"], request.content))
        return httpx.Response(201)

    data = bytes(range(65))
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        TikTokClient(client, "access").upload(
            InboxUpload("mock-id", "https://upload.example.test/"),
            UploadPlan(len(data)),
            BytesIO(data),
        )
    assert requests == [
        (f"bytes {start}-{start + 9}/65", data[start : start + 10])
        for start in range(0, 50, 10)
    ] + [("bytes 50-64/65", data[50:])]


@pytest.mark.parametrize(
    ("statuses", "reason"),
    [
        (["PROCESSING_UPLOAD", "SEND_TO_USER_INBOX"], None),
        (["FAILED"], "file_format_check_failed"),
        (["PROCESSING_UPLOAD"] * 10, None),
    ],
)
def test_sandbox_command_inits_puts_and_polls(
    monkeypatch, tmp_path, capsys, caplog, statuses, reason
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"mock video")
    config = TikTokSettings(
        "sandbox", "key", "secret", "https://example.test/callback/"
    )
    settings = Settings("db", "bot", 1, "key", tiktok=config)
    monkeypatch.setattr(cli.Settings, "from_env", Mock(return_value=settings))
    monkeypatch.setattr(cli, "connect", lambda _: nullcontext(Mock()))
    monkeypatch.setattr(cli.migrate, "up", Mock())
    monkeypatch.setattr(cli, "run_job", lambda name, job, config: job() or 0)
    monkeypatch.setattr(tasks, "TokenCipher", Mock())
    manager = Mock()
    manager.access_token.return_value = "mock-access"
    monkeypatch.setattr(tasks, "TikTokTokenManager", Mock(return_value=manager))
    sleep = Mock()
    monkeypatch.setattr(tasks.time, "sleep", sleep)
    requests = []
    pending = iter(statuses)

    def respond(request):
        requests.append((request.method, request.url.path))
        if request.method == "PUT":
            assert str(request.url) == "https://upload.example.test/send?signature=mock"
            assert "authorization" not in request.headers
            assert request.headers["content-type"] == "video/mp4"
            assert request.headers["content-range"] == "bytes 0-9/10"
            assert request.content == b"mock video"
            return httpx.Response(201)
        assert request.url.host == "open.tiktokapis.com"
        assert request.headers["authorization"] == "Bearer mock-access"
        body = json.loads(request.content)
        if request.url.path.endswith("/init/"):
            assert body == {
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": 10,
                    "chunk_size": 10,
                    "total_chunk_count": 1,
                },
            }
            data = {
                "publish_id": "mock-id",
                "upload_url": "https://upload.example.test/send?signature=mock",
            }
        else:
            assert body == {"publish_id": "mock-id"}
            data = {"status": next(pending), "fail_reason": reason}
        return httpx.Response(200, json={"data": data, "error": {"code": "ok"}})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(tasks.httpx, "Client", lambda: client)
    with caplog.at_level(logging.INFO, logger="httpx"):
        assert cli.main(["tiktok-sandbox-check", str(video)]) == 0
    assert "signature=mock" not in caplog.text
    manager.access_token.assert_called_once()
    assert requests == [
        ("POST", "/v2/post/publish/inbox/video/init/"),
        ("PUT", "/send"),
        *[("POST", "/v2/post/publish/status/fetch/")] * len(statuses),
    ]
    assert sleep.call_count == len(statuses) - 1
    assert all(call.args == (6,) for call in sleep.call_args_list)
    assert (
        capsys.readouterr().out
        == f"publish_id=mock-id, status={statuses[-1]}, fail_reason={reason or '-'}\n"
    )


def test_api_error_keeps_tiktok_error_code():
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": "unaudited_client_can_only_post_to_private_accounts",
                    "message": "secret-ish detail",
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        api = TikTokClient(client, "access")
        with pytest.raises(TikTokAPIError) as caught:
            api.init_inbox(UploadPlan(10))
    assert "unaudited_client_can_only_post_to_private_accounts" in str(caught.value)
    assert "secret-ish" not in str(caught.value)
