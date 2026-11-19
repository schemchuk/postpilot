from contextlib import nullcontext
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest

from postpilot.ai.prompts import SCRIPT_SYSTEM_PROMPT
from postpilot.core.models import Variant
from postpilot.notify.telegram import Notifier
from postpilot.store.repo import ItemRepo
from postpilot.video import pipeline
from postpilot.video.budget import check_budget
from postpilot.video.heygen import HeyGenClient, HeyGenError, VideoResult
from postpilot.video.script import ScriptError, generate_script
from postpilot.video.store import VideoStore

NOW = datetime(2026, 11, 19, tzinfo=UTC)
SCRIPT = " ".join(["Ich habe diese Woche an meinem Projekt gearbeitet."] * 11)


@pytest.mark.parametrize(
    "response",
    [
        " ",
        "Zu kurz.",
        "Wort " * 121,
        *(
            f"{SCRIPT} {x}"
            for x in (
                "https://example.test",
                "www.example.test",
                "example.de",
                "#Projekt",
                "😀",
                "1️⃣",
            )
        ),
    ],
)
def test_script_validation(response):
    with pytest.raises(ScriptError):
        generate_script(Mock(complete=Mock(return_value=response)), "Post")


def test_script_uses_german_variant():
    model = Mock(complete=Mock(return_value=f"  {SCRIPT}\n"))
    assert generate_script(model, "Mein Projekt") == SCRIPT
    model.complete.assert_called_once_with(SCRIPT_SYSTEM_PROMPT, "Mein Projekt")


@pytest.mark.parametrize(
    "count,balance,allowed,reason",
    [
        (4, 20, False, "monthly video cap reached"),
        (3, 2, False, "HeyGen wallet balance below minimum"),
        (3, 3, True, "within budget"),
    ],
)
def test_budget(count, balance, allowed, reason):
    store = Mock(spec=VideoStore)
    store.count_this_month.return_value = count
    heygen = Mock(spec=HeyGenClient)
    heygen.wallet_balance.return_value = balance
    decision = check_budget(store, heygen, NOW, 4, 3)
    assert (decision.allowed, decision.reason) == (allowed, reason)
    store.count_this_month.assert_called_once_with(NOW)
    assert heygen.wallet_balance.call_count == (count < 4)


@pytest.fixture
def dependencies(monkeypatch):
    calls = Mock()
    store = Mock(spec=VideoStore)
    store.render_lock.return_value = nullcontext(True)
    store.rendering_ids.return_value = []
    store.count_this_month.return_value = 0
    store.claim.return_value = uuid4()
    repo = Mock(spec=ItemRepo)
    repo.next_video_variant.return_value = Variant(uuid4(), "de", "Mein Projekt")
    heygen = Mock(spec=HeyGenClient)
    heygen.wallet_balance.return_value = 3
    heygen.create_video.return_value = "provider-id"
    heygen.wait_for_video.return_value = VideoResult(
        "captioned-url", None, None, None, 35
    )
    heygen.download.return_value = b"captioned mp4"
    model = Mock(complete=Mock(return_value=SCRIPT))
    notifier = Mock(spec=Notifier)
    burn = Mock(return_value=b"labelled mp4")
    monkeypatch.setattr(pipeline, "burn_ai_label", burn)
    for name, mock in (
        ("store", store),
        ("repo", repo),
        ("heygen", heygen),
        ("notifier", notifier),
        ("burn", burn),
    ):
        calls.attach_mock(mock, name)
    return calls, (store, repo, heygen, model, notifier, lambda: NOW)


def test_render_one_attaches_labelled_video_before_preview(dependencies):
    calls, deps = dependencies
    pipeline.render_one(*deps, cap=4, min_balance=3)
    render_id = calls.store.claim.return_value
    variant = calls.repo.next_video_variant.return_value
    calls.repo.next_video_variant.assert_called_once_with(retry=False)
    calls.store.claim.assert_called_once_with(variant.item_id, "de", retry=False)
    calls.heygen.create_video.assert_called_once()
    calls.heygen.download.assert_called_once_with("captioned-url")
    calls.burn.assert_called_once_with(b"captioned mp4")
    calls.store.attach.assert_called_once_with(
        render_id, "provider-id", SCRIPT, b"labelled mp4", 35
    )
    calls.notifier.send_video_preview.assert_called_once_with(
        calls.store.get.return_value, b"labelled mp4"
    )
    stages = [call[0] for call in calls.mock_calls]
    assert (
        stages.index("burn")
        < stages.index("store.attach")
        < stages.index("notifier.send_video_preview")
    )
    calls.store.approve.assert_not_called()


def test_render_failure_marks_failed_and_sanitizes_alert(dependencies):
    calls, deps = dependencies
    calls.heygen.wait_for_video.side_effect = HeyGenError("sensitive provider details")
    pipeline.render_one(*deps, cap=4, min_balance=3, retry=True)
    render_id = calls.store.claim.return_value
    calls.repo.next_video_variant.assert_called_once_with(retry=True)
    assert calls.store.claim.call_args.kwargs == {"retry": True}
    calls.store.mark_failed.assert_called_once_with(render_id)
    calls.notifier.send_alert.assert_called_once_with(
        f"Video render {render_id} failed during HeyGen rendering."
    )
    calls.notifier.send_video_preview.assert_not_called()


@pytest.mark.parametrize("stuck", [False, True])
def test_no_eligible_item_even_if_render_stuck(dependencies, stuck):
    calls, deps = dependencies
    calls.repo.next_video_variant.return_value = None
    if stuck:
        render_id = uuid4()
        calls.store.rendering_ids.return_value = [render_id]
    pipeline.render_one(*deps, cap=4, min_balance=3)
    calls.store.claim.assert_not_called()
    assert not calls.heygen.mock_calls
    if stuck:
        calls.notifier.send_alert.assert_called_once_with(
            f"1 video render(s) stuck in RENDERING: {render_id}"
        )
    else:
        calls.notifier.send_alert.assert_not_called()


def test_budget_refusal_alerts_before_claim(dependencies):
    calls, deps = dependencies
    calls.store.count_this_month.return_value = 4
    pipeline.render_one(*deps, cap=4, min_balance=3)
    calls.store.claim.assert_not_called()
    assert not calls.heygen.mock_calls
    calls.notifier.send_alert.assert_called_once_with(
        "Video rendering blocked: monthly video cap reached."
    )


def test_preview_failure_marks_ready_render_failed(dependencies):
    calls, deps = dependencies
    calls.notifier.send_video_preview.side_effect = RuntimeError("sensitive details")
    pipeline.render_one(*deps, cap=4, min_balance=3)
    render_id = calls.store.claim.return_value
    calls.store.mark_failed.assert_called_once_with(render_id, preview=True)
    calls.notifier.send_alert.assert_called_once_with(
        f"Video render {render_id} failed during Telegram preview."
    )
