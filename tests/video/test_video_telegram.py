from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from postpilot.notify import telegram
from postpilot.notify.approval import ApprovalHandler
from postpilot.store.repo import ItemRepo
from postpilot.video.store import VideoStore


def test_send_video_multipart():
    def respond(request):
        assert request.url.path.endswith("/sendVideo")
        assert request.headers["content-type"].startswith("multipart/form-data;")
        body = request.read()
        assert b'name="chat_id"\r\n\r\n123' in body
        assert b'name="caption"\r\n\r\nPreview' in body
        assert b'name="supports_streaming"\r\n\r\ntrue' in body
        assert b'name="reply_markup"\r\n\r\n{"inline_keyboard": []}' in body
        assert b'name="video"; filename="preview.mp4"' in body
        assert b"Content-Type: video/mp4\r\n\r\nlabelled video" in body
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        telegram.TelegramApi("test", client=client).send_video(
            123, b"labelled video", "Preview", {"inline_keyboard": []}
        )


def test_notifier_preview_and_oversized_alert(monkeypatch):
    monkeypatch.setattr(telegram, "_VIDEO_LIMIT", 7)
    api = Mock(spec=telegram.TelegramApi)
    notifier = telegram.Notifier(api, 123)
    render = Mock(id=uuid4())
    notifier.send_video_preview(render, b"1234567")
    notifier.send_video_preview(render, b"12345678")
    api.send_video.assert_called_once_with(
        123,
        b"1234567",
        "AI-generated avatar video (label burned in) - approve to allow Instagram/Facebook",
        {
            "inline_keyboard": [
                [
                    {"text": "Approve", "callback_data": f"vapprove:{render.id}"},
                    {"text": "Reject", "callback_data": f"vreject:{render.id}"},
                ]
            ]
        },
    )
    api.send_message.assert_called_once_with(
        123, f"Video render {render.id} exceeds Telegram's 50 MB upload limit."
    )


@pytest.mark.parametrize(
    "action,user,ready,label",
    [
        ("vapprove", 123, True, "Video approved"),
        ("vreject", 123, True, "Video rejected"),
        ("vapprove", 456, True, None),
        ("vreject", 456, True, None),
        ("vapprove", 123, False, None),
        ("vreject", 123, False, None),
    ],
)
def test_video_callback(action, user, ready, label):
    api = Mock(spec=telegram.TelegramApi)
    store = Mock(spec=VideoStore)
    repo = Mock(spec=ItemRepo)
    store.approve.return_value = ready
    store.reject.return_value = ready
    render_id = uuid4()
    handler = ApprovalHandler(repo, api, 123, video_store=store)
    handler.handle_update(
        {
            "callback_query": {
                "id": "callback",
                "from": {"id": user},
                "message": {
                    "chat": {"id": 123},
                    "message_id": 42,
                    "video": {"file_id": "preview"},
                    "reply_markup": {
                        "inline_keyboard": [
                            [{"callback_data": f"{action}:{render_id}"}]
                        ]
                    },
                },
                "data": f"{action}:{render_id}",
            }
        }
    )

    assert not repo.mock_calls
    if user != 123:
        assert not store.mock_calls
        assert not api.mock_calls
        return
    operation = store.approve if action == "vapprove" else store.reject
    assert operation.call_count == 1
    assert operation.call_args.args[:2] == (render_id, user)
    api.answer_callback_query.assert_called_once_with(
        "callback", None if ready else "Already handled"
    )
    if label:
        api.edit_message_caption.assert_called_once_with(123, 42, label)
    else:
        api.edit_message_caption.assert_not_called()


def test_video_callback_requires_preview():
    api = Mock(spec=telegram.TelegramApi)
    store = Mock(spec=VideoStore)
    handler = ApprovalHandler(Mock(spec=ItemRepo), api, 123, video_store=store)
    handler.handle_update(
        {
            "callback_query": {
                "id": "callback",
                "from": {"id": 123},
                "message": {
                    "chat": {"id": 123},
                    "message_id": 42,
                    "text": "Oversized video alert",
                },
                "data": f"vapprove:{uuid4()}",
            }
        }
    )
    store.approve.assert_not_called()
    api.answer_callback_query.assert_called_once_with("callback", "Already handled")
