from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from postpilot.core.models import Item
from postpilot.core.states import ItemState
from postpilot.core.tiktok import TikTokChoice, TikTokSelection
from postpilot.notify.approval import ApprovalHandler
from postpilot.notify.telegram import tiktok_rows
from postpilot.video.tiktok import CreatorInfo

CREATOR = CreatorInfo(
    [
        "PUBLIC_TO_EVERYONE",
        "MUTUAL_FOLLOW_FRIENDS",
        "FOLLOWER_OF_CREATOR",
        "SELF_ONLY",
        "unknown",
    ],
    False,
    True,
    False,
    60,
    "Creator",
)
NOW = datetime.now(UTC)


def test_tiktok_privacy_and_toggle_rows(caplog):
    item_id = uuid4()
    rows = tiktok_rows(item_id, CREATOR, TikTokSelection())
    assert rows == [
        [
            {"text": label, "callback_data": f"ttp:{code}:{item_id}"}
            for label, code in (
                ("Public", "pub"),
                ("Friends", "fri"),
                ("Followers", "fol"),
                ("Only me", "me"),
            )
        ],
        [
            {"text": "Comments: off", "callback_data": f"ttt:c:{item_id}"},
            {"text": "Stitch: off", "callback_data": f"ttt:s:{item_id}"},
        ],
    ]
    assert all(
        len(button["callback_data"].encode()) <= 64 for row in rows for button in row
    )
    assert caplog.messages == ["Unknown TikTok privacy option"]
    chosen = tiktok_rows(
        item_id, CREATOR, TikTokSelection("SELF_ONLY", allow_stitch=True)
    )
    assert chosen[0][-1]["text"] == "✓ Only me"
    assert chosen[1][-1]["text"] == "Stitch: on"


@pytest.fixture
def preview():
    repo, api, project, settings = Mock(), Mock(), Mock(), Mock()
    project.lock_item.side_effect = nullcontext
    project.resolve_preview.return_value = True
    item = Item(uuid4(), "own_video", ItemState.AWAITING_APPROVAL, NOW, NOW, None, None)
    repo.get_item.return_value = item
    settings.get.return_value = None
    handler = ApprovalHandler(
        repo, api, 42, project_store=project, tiktok_settings=settings
    )

    def press(
        action,
        *,
        chat=42,
        source="own_video",
        state=ItemState.AWAITING_APPROVAL,
        direct=True,
        update_id=None,
    ):
        repo.get_item.return_value = replace(item, source=source, state=state)
        approve = "approve" if source == "own_video" else "papprove"
        other = "edit" if source == "own_video" else "preject"
        rows = [
            [{"text": a, "callback_data": f"{a}:{item.id}"} for a in (approve, other)]
        ]
        if direct:
            rows.extend(tiktok_rows(item.id, CREATOR, TikTokSelection()))
        handler.handle_update(
            {
                "update_id": update_id,
                "callback_query": {
                    "id": "callback",
                    "from": {"id": 42},
                    "data": f"{action}:{item.id}",
                    "message": {
                        "chat": {"id": chat},
                        "message_id": 73,
                        "reply_markup": {"inline_keyboard": rows},
                    },
                },
            }
        )
        return rows

    return SimpleNamespace(
        press=press, item=item, repo=repo, api=api, project=project, settings=settings
    )


@pytest.mark.parametrize(
    ("action", "state", "method", "value", "row", "button", "text"),
    [
        (
            "ttp:me",
            TikTokSelection("SELF_ONLY"),
            "set_privacy",
            "SELF_ONLY",
            1,
            3,
            "✓ Only me",
        ),
        (
            "ttt:c",
            TikTokSelection(allow_comment=True),
            "flip_toggle",
            "comment",
            2,
            0,
            "Comments: on",
        ),
    ],
)
def test_choice_saves_and_redraws(
    preview, action, state, method, value, row, button, text
):
    preview.settings.get_partial.return_value = state
    original = preview.press(action)
    call = getattr(preview.settings, method).call_args
    assert call.args[:2] == (preview.item.id, value)
    preview.api.answer_callback_query.assert_called_once_with("callback", None)
    original[row][button] = {**original[row][button], "text": text}
    preview.api.edit_message_reply_markup.assert_called_once_with(
        42, 73, {"inline_keyboard": original}
    )


@pytest.mark.parametrize(("action", "chat"), [("ttt:d", 42), ("ttp:me", 999)])
def test_unoffered_button_or_wrong_chat_is_ignored(preview, action, chat):
    preview.press(action, chat=chat)
    preview.repo.get_item.assert_not_called()
    assert preview.settings.mock_calls == preview.api.mock_calls == []


def test_closed_own_video_refuses_choice(preview):
    preview.press("ttp:me", state=ItemState.APPROVED)
    preview.api.answer_callback_query.assert_called_once_with(
        "callback", "Already handled"
    )
    assert preview.settings.mock_calls == []


def test_released_project_locks_choice_and_removes_only_tiktok_rows(preview):
    preview.project.get_by_item.return_value = SimpleNamespace(
        release_at=NOW, approval="auto"
    )
    original = preview.press("ttp:me", source="project", state=ItemState.APPROVED)
    preview.api.answer_callback_query.assert_called_once_with(
        "callback", "TikTok choice locked at release time"
    )
    preview.api.edit_message_reply_markup.assert_called_once_with(
        42, 73, {"inline_keyboard": original[:1]}
    )
    assert preview.settings.mock_calls == []


def test_auto_project_can_choose_before_release(preview):
    release_at = NOW + timedelta(days=1)
    preview.project.get_by_item.return_value = SimpleNamespace(
        release_at=release_at, approval="auto"
    )
    preview.settings.get_partial.return_value = TikTokSelection("SELF_ONLY")
    preview.press("ttp:me", source="project", state=ItemState.APPROVED)
    item_id, privacy, chosen_at = preview.settings.set_privacy.call_args.args
    assert (
        item_id == preview.item.id and privacy == "SELF_ONLY" and chosen_at < release_at
    )


@pytest.mark.parametrize(
    ("action", "source"), [("approve", "own_video"), ("papprove", "project")]
)
def test_approve_requires_privacy_when_offered(preview, action, source):
    if source == "own_video":
        transition = preview.repo.transition
    else:
        transition = preview.project.resolve_preview
    preview.press(action, source=source)
    preview.api.answer_callback_query.assert_called_once_with(
        "callback", "Choose a TikTok privacy level first"
    )
    preview.api.edit_message_reply_markup.assert_not_called()
    preview.api.edit_message_text.assert_not_called()
    transition.assert_not_called()

    preview.settings.get.return_value = TikTokChoice("SELF_ONLY")
    preview.press(action, source=source)
    transition.assert_called_once()
    if source == "own_video":
        preview.api.edit_message_text.assert_called_once_with(
            42, 73, "Approved", reply_markup={"inline_keyboard": []}
        )
    else:
        preview.api.edit_message_reply_markup.assert_called_once_with(
            42, 73, {"inline_keyboard": []}
        )


@pytest.mark.parametrize(
    ("action", "source"), [("approve", "own_video"), ("papprove", "project")]
)
def test_preview_without_tiktok_buttons_can_be_approved(preview, action, source):
    preview.press(action, source=source, direct=False)
    preview.settings.get.assert_not_called()
    if source == "own_video":
        preview.repo.transition.assert_called_once()
    else:
        preview.project.resolve_preview.assert_called_once()


def test_replayed_toggle_does_not_flip_twice(preview, monkeypatch):
    from postpilot.store.update_inbox import UpdateInbox

    mark_processed = Mock(side_effect=[True, False])
    monkeypatch.setattr(UpdateInbox, "mark_processed", mark_processed)
    preview.settings.get_partial.return_value = TikTokSelection(allow_comment=True)
    preview.press("ttt:c", update_id=42)
    preview.press("ttt:c", update_id=42)
    assert mark_processed.call_count == 2
    preview.settings.flip_toggle.assert_called_once()
    preview.api.edit_message_reply_markup.assert_called_once()
