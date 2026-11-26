import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest

from postpilot.core.models import Item, Variant
from postpilot.core.states import ItemState
from postpilot.notify.approval import ApprovalHandler
from postpilot.notify.telegram import TelegramError
from postpilot.sources.own_video import WORK, OwnVideoInbox
from postpilot.store.own_videos import OwnVideo, OwnVideoIntake


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


@pytest.mark.parametrize("send_fails", [False, True])
def test_stale_own_video_callback_replies_to_button(send_fails, caplog):
    repo, api = MagicMock(), Mock()
    inbox = OwnVideoInbox(repo, api, None)
    inbox.store = Mock()
    inbox.store.get.return_value = None
    if send_fails:
        api.send_message.side_effect = TelegramError("private request details")
    handler = ApprovalHandler(repo, api, 12, own_video=inbox)

    handler.handle_update(
        {
            "update_id": 1,
            "callback_query": {
                "id": "callback",
                "from": {"id": 12},
                "message": {"chat": {"id": 12}, "message_id": 42},
                "data": f"own:skip:{uuid4()}",
            },
        }
    )

    api.answer_callback_query.assert_not_called()
    api.send_message.assert_called_once_with(
        12, "Already handled", reply_to_message_id=42
    )
    assert caplog.messages == (["Telegram callback reply failed"] if send_fails else [])


@pytest.mark.parametrize("preview_fails", [False, True])
def test_regen_attempts_preview_after_edit_failure(preview_fails, caplog):
    repo, api = MagicMock(), Mock()
    inbox = OwnVideoInbox(repo, api, None)
    inbox.store = Mock()
    now = datetime.now(UTC)
    item = Item(uuid4(), "own_video", ItemState.AWAITING_APPROVAL, now, now, None, None)
    video = OwnVideo(uuid4(), item.id, 12, "file", 30, False, "Interview", "generated")
    inbox.store.get.return_value = video
    inbox._generate = Mock(return_value=(item, ()))
    api.edit_message_text.side_effect = TelegramError("Edit failed")
    failure = TelegramError("Preview delivery failed")

    def deliver_preview(*args):
        api.edit_message_text.assert_called_once_with(12, 42, "Regenerated")
        if preview_fails:
            raise failure

    inbox._preview = Mock(side_effect=deliver_preview)
    if preview_fails:
        with pytest.raises(TelegramError) as caught:
            inbox.callback(1, 12, 42, "regen", item.id)
        assert caught.value is failure
    else:
        inbox.callback(1, 12, 42, "regen", item.id)

    inbox._preview.assert_called_once_with(video, item, ())
    assert caplog.messages == ["Telegram callback reply failed"]


def test_guided_intake_takes_plain_reply_before_engagement_and_s2(monkeypatch):
    repo, api = MagicMock(), Mock()
    clock = Mock(return_value=1000.0)
    own = OwnVideoInbox(repo, api, None, clock=clock)
    own.store = Mock()
    own.store.has_update.return_value = False
    own.store.get.return_value = None
    own.store.get_intake.return_value = OwnVideoIntake(uuid4().hex, "kind", 1000.0)
    inbox = Mock()
    handler = ApprovalHandler(repo, api, 12, inbox=inbox, own_video=own)
    engagement_edit = Mock(return_value=False)
    monkeypatch.setattr(handler, "_edit_engagement_reply", engagement_edit)
    repo.has_inbox_update.return_value = False
    repo.apply_pending_edit.return_value = None

    update = {
        "update_id": 1,
        "message": {
            "from": {"id": 12},
            "chat": {"id": 12},
            "reply_to_message": {"message_id": 42},
            "text": "Plain reply",
        },
    }
    handler.handle_update(update)
    api.send_message.assert_called_once_with(12, "What kind of video?")
    own.store.claim_update.assert_called_once_with(1)
    engagement_edit.assert_not_called()
    inbox.handle_text.assert_not_called()

    clock.return_value += 3600
    handler.handle_update({**update, "update_id": 2})
    engagement_edit.assert_called_once_with(12, 42, "Plain reply")
    inbox.handle_text.assert_called_once_with(2, 12, "Plain reply")


def test_guided_intake_leaves_commands_available(monkeypatch):
    repo, api = MagicMock(), Mock()
    own = OwnVideoInbox(repo, api, None, clock=lambda: 0.0)
    own.store = Mock()
    own.store.get_intake.return_value = OwnVideoIntake(uuid4().hex, "text", 0.0)
    stats, renewal = Mock(), Mock()
    handler = ApprovalHandler(
        repo, api, 12, own_video=own, stats=stats, renewal=renewal
    )
    profile, series = Mock(), Mock()
    monkeypatch.setattr(handler, "_handle_profile_command", profile)
    monkeypatch.setattr(handler, "_handle_series_command", series)
    monkeypatch.setattr("postpilot.notify.approval.format_stats", lambda _: "Stats")

    for update_id, command in enumerate(("/stats", "/profile", "/series", "/renew"), 1):
        handler.handle_update(
            {
                "update_id": update_id,
                "message": {
                    "from": {"id": 12},
                    "chat": {"id": 12},
                    "message_id": update_id,
                    "reply_to_message": {"message_id": 42},
                    "text": command,
                },
            }
        )

    stats.read.assert_called_once()
    profile.assert_called_once_with(12, "profile", "")
    series.assert_called_once_with(12, "series", [])
    renewal.handle_message.assert_called_once_with(12, 4, "/renew")
    own.store.claim_update.assert_not_called()
