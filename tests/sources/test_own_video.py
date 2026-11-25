import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest

from postpilot.core.models import Item, Variant
from postpilot.core.states import ItemState
from postpilot.notify.approval import ApprovalHandler
from postpilot.sources.own_video import WORK, OwnVideoInbox
from postpilot.store.own_videos import OwnVideo


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


@pytest.mark.parametrize("rule_clean", [True, False])
def test_own_video_humanizer_preview(rule_clean):
    repo, api, model = MagicMock(), Mock(), Mock()
    inbox = OwnVideoInbox(repo, api, model)
    inbox.store = Mock()
    video = OwnVideo(uuid4(), None, 12, "file", 30, False, "Мій текст", "collecting")
    inbox.store.get.return_value = video
    now = datetime.now(UTC)
    item = Item(uuid4(), "own_video", ItemState.AWAITING_APPROVAL, now, now, None, None)
    repo.create_item.return_value = repo.transition.return_value = item
    de = "Ich habe einen Test ergänzt.\n#Python #Test #GitHub #Code"
    ua = "Я додав тест.\n#Python #Тест #GitHub #Код"
    candidate = de if rule_clean else de.replace("ergänzt.", "ergänzt — fertig.")
    failed = json.dumps(
        {"ai_score": 5, "passed": False, "issues": [], "unsupported_facts": []}
    )
    passed = json.dumps(
        {"ai_score": 2, "passed": True, "issues": [], "unsupported_facts": []}
    )
    model.complete.side_effect = [
        json.dumps({"de": de, "ua": ua}),
        *([candidate, failed] * 3),
        ua,
        passed,
    ]
    repo.get_variants.return_value = [
        Variant(item.id, "de", de),
        Variant(item.id, "ua", ua),
    ]

    assert inbox.message(
        1, 12, {"video": {"file_id": "file"}, "caption": "/work Мій текст"}
    )

    if rule_clean:
        repo.create_item.assert_called_once_with("own_video")
        repo.add_variant.assert_any_call(item.id, "de", de)
        repo.add_variant.assert_any_call(item.id, "ua", ua)
        preview = api.send_message.call_args.args[1]
        assert "Humanizer DE: not passed (score 5), check the text" in preview
        assert "Humanizer UA" not in preview
        inbox.preview(12, item)
        assert "Humanizer DE" not in api.send_message.call_args.args[1]
    else:
        repo.create_item.assert_not_called()
        api.send_video.assert_not_called()
        api.send_message.assert_called_with(
            12, "Generation failed (humanizer), press Generate again."
        )
