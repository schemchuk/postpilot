from unittest.mock import Mock

import pytest

from postpilot.notify.approval import ApprovalHandler
from postpilot.sources.own_video import WORK


@pytest.mark.parametrize(
    ("caption", "ai", "text"),
    [
        ("/work", None, None),
        ("/work ai", "ai", None),
        ("/work text", None, "text"),
        ("/work ai text\nmore", "ai", "text\nmore"),
        ("/work@bot", None, None),
        ("/work airline", None, "airline"),
        ("/work text ai", None, "text ai"),
    ],
)
def test_caption(caption, ai, text):
    assert WORK.fullmatch(caption).groups() == (ai, text)


def test_foreign_sender_cannot_use_video_intake():
    intake = Mock()
    handler = ApprovalHandler(Mock(), Mock(), 12, own_video=intake)
    handler.handle_update(
        {
            "update_id": 1,
            "message": {
                "from": {"id": 13},
                "chat": {"id": 12},
                "video": {"file_id": "test"},
                "caption": "/work",
            },
        }
    )
    intake.message.assert_not_called()
