from collections.abc import Callable
from datetime import datetime

from postpilot.ai.client import TextModel
from postpilot.notify.telegram import Notifier
from postpilot.store.repo import ItemRepo
from postpilot.video.budget import check_budget
from postpilot.video.heygen import HeyGenClient, HeyGenError
from postpilot.video.label import burn_ai_label
from postpilot.video.script import generate_script
from postpilot.video.store import VideoStore


def render_one(
    store: VideoStore,
    repo: ItemRepo,
    heygen: HeyGenClient,
    model: TextModel,
    notifier: Notifier,
    clock: Callable[[], datetime],
    *,
    cap: int,
    min_balance: float,
    retry: bool = False,
) -> None:
    with store.render_lock() as acquired:
        if not acquired:
            return
        stuck = store.rendering_ids()
        if stuck:
            ids = ", ".join(map(str, stuck[:5]))
            notifier.send_alert(
                f"{len(stuck)} video render(s) stuck in RENDERING: {ids}"
            )

        variant = repo.next_video_variant(retry=retry)
        if variant is None:
            return
        try:
            budget = check_budget(store, heygen, clock(), cap, min_balance)
        except HeyGenError:
            notifier.send_alert("Video rendering blocked: HeyGen wallet check failed.")
            return
        if not budget.allowed:
            notifier.send_alert(f"Video rendering blocked: {budget.reason}.")
            return

        render_id = store.claim(variant.item_id, variant.lang, retry=retry)
        if render_id is None:
            return
        stage = "script generation"
        attached = False
        try:
            script = generate_script(model, variant.text)
            stage = "HeyGen rendering"
            remote_id = heygen.create_video(script, f"PostPilot {variant.item_id}")
            result = heygen.wait_for_video(remote_id)
            stage = "download and AI label"
            video = burn_ai_label(heygen.download(result.captioned_video_url))
            stage = "video storage"
            attached = store.attach(
                render_id, remote_id, script, video, result.duration
            )
            if not attached:
                raise RuntimeError("Video claim lost")
            render = store.get(render_id)
            if render is None:
                raise RuntimeError("Video render missing")
            stage = "Telegram preview"
            notifier.send_video_preview(render, video)
        except Exception:  # noqa: BLE001 - record and report any render failure
            try:
                if attached:
                    store.mark_failed(render_id, preview=True)
                else:
                    store.mark_failed(render_id)
            finally:
                notifier.send_alert(f"Video render {render_id} failed during {stage}.")
