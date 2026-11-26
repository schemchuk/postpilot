import json
import logging
import re
from collections.abc import Callable
from time import time
from typing import Any
from uuid import UUID, uuid4

from postpilot.ai.client import TextModel
from postpilot.ai.humanizer import humanize_pair
from postpilot.ai.prompts import OWN_VIDEO_SYSTEM_PROMPT, interview_message
from postpilot.core.limits import (
    LINKEDIN_TEXT_LIMIT,
    REELS_MAX_DURATION,
    REELS_MIN_DURATION,
)
from postpilot.core.models import Item
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.notify.telegram import (
    Notifier,
    TelegramApi,
    TelegramError,
    approval_keyboard,
    send_callback_result,
)
from postpilot.store.own_videos import OwnVideo, OwnVideoIntake, OwnVideoStore
from postpilot.store.repo import ItemRepo

_LOG = logging.getLogger(__name__)

WORK = re.compile(
    r"^/work(?:@[a-zA-Z0-9_]+)?(?:\s+(?P<ai>ai)(?=\s|$))?(?:\s+(?P<text>.*))?$",
    re.DOTALL,
)
_INTERVIEW_LIMIT = 20_000
_FILE_LIMIT = 20_000_000
_INTAKE_TTL = 60 * 60
_INTAKE_PROMPTS = {
    "kind": ("What kind of video?", (("Original", "original"), ("AI-generated", "ai"))),
    "video": ("Send the video (MP4 up to 20 MB; Reels need 3-90 s).", ()),
    "description": (
        "Video received.",
        (("Upload description", "upload"), ("No description", "none")),
    ),
    "text": (
        "Send the text as messages or a .txt file, then press Done.",
        (("Done", "done"),),
    ),
}
_INTAKE_HINTS = {"description": "Choose Upload description, No description, or Cancel."}
_AWAITING = ItemState.AWAITING_APPROVAL
_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"ua": {"type": "string"}, "de": {"type": "string"}},
    "required": ["ua", "de"],
    "additionalProperties": False,
}


def _is_text_file(doc: dict[str, Any]) -> bool:
    return bool(
        doc.get("mime_type") == "text/plain"
        or doc.get("file_name", "").lower().endswith(".txt")
    )


class OwnVideoInbox:
    def __init__(
        self,
        repo: ItemRepo,
        api: TelegramApi,
        model: TextModel | None,
        *,
        clock: Callable[[], float] = time,
    ) -> None:
        self.repo, self.api, self.model = repo, api, model
        self.store = OwnVideoStore(repo.conn)
        self.clock = clock

    def message(self, update_id: int, chat: int, message: dict[str, Any]) -> bool:
        text = message.get("text", "")
        doc = message.get("document") or {}
        caption = WORK.fullmatch(message.get("caption", ""))
        media = message.get("video") or (
            doc if doc.get("mime_type") == "video/mp4" else None
        )
        reply_id = (message.get("reply_to_message") or {}).get("message_id")
        work = WORK.fullmatch(text.rstrip())
        command = caption or work
        bare = work is not None and not (work["ai"] or work["text"])
        plain = bool(text) and not text.lstrip().startswith("/")
        uncaptioned = not message.get("caption")
        if not (command or plain or (uncaptioned and (media or doc))):
            return False
        generated = None
        video = None
        with self.repo.conn.transaction():
            if caption and media:
                if self.store.claim_update(update_id):
                    video = self._add(
                        chat,
                        media,
                        bool(caption["ai"]),
                        (caption["text"] or "").strip(),
                    )
                    if video and video.interview:
                        generated = self._generate(video, message.get("message_id"))
            elif bare:
                self.store.lock_intake(chat)
                if self.store.claim_update(update_id):
                    previous = self.store.get_intake(chat)
                    if previous and previous.step != "closed":
                        self._close_intake(chat, previous)
                    self._show_step(
                        chat, OwnVideoIntake(uuid4().hex, "kind", self.clock())
                    )
            elif command:
                if self.store.claim_update(update_id):
                    self.api.send_message(
                        chat, "Send a video with caption: /work [ai] [interview text]"
                    )
            elif (
                (plain or doc)
                and type(reply_id) is int
                and (video := self.store.get(chat, prompt_message_id=reply_id))
            ):
                if video.status == "collecting" and self.store.claim_update(update_id):
                    self._append(video, text, doc)
            else:
                self.store.lock_intake(chat)
                # A replay must stay consumed even after the intake closes or expires.
                if self.store.has_update(update_id):
                    return True
                intake = self.store.get_intake(chat)
                if intake is None or intake.step == "closed" or self._expired(intake):
                    return False
                take_video = (
                    intake.step == "video"
                    and media
                    and uncaptioned
                    and reply_id is None
                )
                take_file = intake.step == "text" and uncaptioned and _is_text_file(doc)
                if not (plain or take_video or take_file):
                    return False
                if self.store.claim_update(update_id):
                    if take_video:
                        assert media is not None
                        id = self._store_video(chat, media, intake.ai_generated, "")
                        if id is not None:
                            intake.video_id = str(id)
                            intake.step = "description"
                            self._show_step(chat, intake)
                    elif intake.step == "text":
                        video = self._intake_video(chat, intake)
                        if self._append(video, text, doc):
                            self._save_step(chat, intake)
                    else:
                        hint = _INTAKE_HINTS.get(
                            intake.step, _INTAKE_PROMPTS[intake.step][0]
                        )
                        self.api.send_message(chat, hint)
        if generated and video:
            self._preview(video, *generated)
        return True

    def _store_video(
        self, chat: int, media: dict[str, Any], ai: bool, text: str
    ) -> UUID | None:
        if (media.get("file_size") or 0) > _FILE_LIMIT:
            self.api.send_message(
                chat, "Video over 20 MB: send it as video, not as file, or shorten it."
            )
            return None
        id = self.store.add(chat, media, ai, text)
        if id is None:
            self.api.send_message(chat, "This video was already added.")
            return None
        return id

    def _add(
        self, chat: int, media: dict[str, Any], ai: bool, text: str
    ) -> OwnVideo | None:
        id = self._store_video(chat, media, ai, text)
        if id is None:
            return None
        prompt = self.api.send_message(
            chat,
            "Video received. Reply with the interview text (message or .txt), then press Generate.",
            {
                "inline_keyboard": [
                    [
                        {"text": label, "callback_data": f"own:{action}:{id}"}
                        for label, action in (("Generate", "gen"), ("Skip", "skip"))
                    ]
                ]
            },
        )
        self.store.set_prompt(id, prompt)
        return self.store.get(chat, id=id)

    def _append(self, video: OwnVideo, text: str, doc: dict[str, Any]) -> bool:
        chat = video.chat_id
        if _is_text_file(doc):
            try:
                text = self.api.download_file(self.api.get_file(doc["file_id"])).decode(
                    "utf-8-sig"
                )
            except (TelegramError, UnicodeError):
                self.api.send_message(
                    chat, "Could not read the file. Send a UTF-8 .txt file."
                )
                return False
        if not text.strip():
            return False
        joined = "\n\n".join(part for part in (video.interview, text.strip()) if part)
        if len(joined) > _INTERVIEW_LIMIT:
            self.api.send_message(chat, "Interview exceeds 20,000 characters.")
            return False
        self.store.set_interview(video.id, joined)
        self.api.send_message(chat, f"Text added ({len(joined)} chars).")
        return True

    def _expired(self, intake: OwnVideoIntake) -> bool:
        return self.clock() - intake.updated_at >= _INTAKE_TTL

    def _save_step(self, chat: int, intake: OwnVideoIntake) -> None:
        intake.updated_at = self.clock()
        self.store.save_intake(chat, intake)

    def _show_step(self, chat: int, intake: OwnVideoIntake) -> None:
        prompt, buttons = _INTAKE_PROMPTS[intake.step]
        buttons = (*buttons, ("Cancel", f"cancel-{intake.step}"))
        self._save_step(chat, intake)
        self.api.send_message(
            chat,
            prompt,
            {
                "inline_keyboard": [
                    [
                        {
                            "text": label,
                            "callback_data": f"own:intake:{action}:{intake.token}",
                        }
                        for label, action in buttons
                    ]
                ]
            },
        )

    def _intake_video(self, chat: int, intake: OwnVideoIntake) -> OwnVideo:
        assert intake.video_id is not None
        video = self.store.get(chat, id=UUID(intake.video_id))
        assert video is not None
        return video

    def _close_intake(self, chat: int, intake: OwnVideoIntake) -> None:
        if intake.video_id:
            self.store.finish(UUID(intake.video_id))
        intake.step = "closed"
        self._save_step(chat, intake)

    def intake_callback(
        self, update_id: int, chat: int, message_id: object, action: str, token: UUID
    ) -> None:
        preview: tuple[OwnVideo, Item, tuple[str, ...]] | None = None
        with self.repo.conn.transaction():
            self.store.lock_intake(chat)
            intake = self.store.get_intake(chat)
            if (
                not self.store.claim_update(update_id)
                or intake is None
                or intake.token != token.hex
                or intake.step == "closed"
            ):
                result = "Already handled"
            elif self._expired(intake):
                result = "Expired, send /work again."
            elif action == f"cancel-{intake.step}":
                self._close_intake(chat, intake)
                if type(message_id) is int:
                    try:
                        self.api.edit_message_text(chat, message_id, "Cancelled")
                        return
                    except TelegramError:
                        _LOG.warning("Telegram callback edit failed")
                result = "Cancelled"
            elif intake.step == "kind" and action in {"original", "ai"}:
                intake.ai_generated = action == "ai"
                intake.step = "video"
                self._show_step(chat, intake)
                return
            elif intake.step == "description" and action == "upload":
                intake.step = "text"
                self._show_step(chat, intake)
                return
            elif intake.step == "description" and action == "none":
                video = self._intake_video(chat, intake)
                if not video.reels_eligible:
                    assert video.duration is not None
                    result = (
                        f"Video is {round(video.duration)} s: Reels need "
                        f"{REELS_MIN_DURATION}-{REELS_MAX_DURATION} s. "
                        "Upload a description to post on LinkedIn."
                    )
                else:
                    item_id = self._create_item(video, ua="", de="")
                    intake.step = "closed"
                    self._save_step(chat, intake)
                    preview = (video, self.repo.transition(item_id, _AWAITING), ())
                    result = None
            elif intake.step == "text" and action == "done":
                video = self._intake_video(chat, intake)
                if video.interview:
                    generated = self._generate(video, message_id, retry_button="Done")
                    if generated is None:
                        return
                    intake.step = "closed"
                    self._save_step(chat, intake)
                    preview = (video, *generated)
                    result = None
                else:
                    result = "Add the text first."
            else:
                result = "Already handled"
        if result is not None:
            send_callback_result(self.api, chat, message_id, result)
        elif preview is not None:
            self._preview(*preview)

    def callback(
        self,
        update_id: int,
        chat: int,
        message_id: object,
        action: str,
        id: UUID,
    ) -> None:
        regen = action == "regen"
        with self.repo.conn.transaction():
            video = self.store.get(chat, **{"item_id" if regen else "id": id})
            if (
                video is None
                or video.status == "skipped"
                or (action == "own:skip" and video.status != "collecting")
                or not self.store.claim_update(update_id)
            ):
                send_callback_result(self.api, chat, message_id, "Already handled")
                return
            if action == "own:skip":
                self.store.finish(video.id)
                if type(message_id) is int:
                    try:
                        self.api.edit_message_text(chat, message_id, "Skipped")
                    except TelegramError:
                        _LOG.warning("Telegram callback reply failed")
                else:
                    send_callback_result(self.api, chat, message_id, "Skipped")
                return
            generated = (
                self._generate(video, message_id)
                if regen or video.item_id is None
                else None
            )
        preview: Item | None
        if generated:
            preview, warnings = generated
        else:
            preview = self.repo.get_item(video.item_id) if video.item_id else None
            warnings = ()
        if preview and preview.state == _AWAITING:
            try:
                if regen and type(message_id) is int:
                    self.api.edit_message_text(
                        chat,
                        message_id,
                        "Regenerated" if generated else "Previous draft kept",
                    )
            except TelegramError:
                _LOG.warning("Telegram callback reply failed")
            self._preview(video, preview, warnings)

    def _generate(
        self, video: OwnVideo, message_id: object, *, retry_button: str = "Generate"
    ) -> tuple[Item, tuple[str, ...]] | None:
        chat = video.chat_id
        if not video.interview:
            send_callback_result(
                self.api, chat, message_id, "Add the interview text first."
            )
            return None
        step = "AI"
        try:
            with self.repo.conn.transaction():
                if video.item_id:
                    self.repo.transition(
                        video.item_id, ItemState.DRAFT, expected_state=_AWAITING
                    )
                if self.model is None:
                    raise ValueError("Missing model")
                raw = self.model.complete(
                    OWN_VIDEO_SYSTEM_PROMPT, interview_message(video.interview), _SCHEMA
                )
                step = "JSON"
                draft = json.loads(raw)
                if (
                    not isinstance(draft, dict)
                    or draft.keys() != {"ua", "de"}
                    or not all(isinstance(value, str) for value in draft.values())
                ):
                    raise ValueError("Invalid post pair")
                step = "length"
                de, ua = draft["de"].strip(), draft["ua"].strip()
                self._check_lengths(de, ua)
                step = "humanizer"
                de_result, ua_result = humanize_pair(
                    self.model, de, ua, {"interview": video.interview}, soft=True
                )
                de, ua = de_result.text, ua_result.text
                warnings = tuple(
                    f"Humanizer {lang}: not passed (score {result.soft_score}), check the text"
                    for lang, result in (("DE", de_result), ("UA", ua_result))
                    if result.soft_score is not None
                )
                step = "length"
                self._check_lengths(de, ua)
                step = "storage"
                if video.item_id:
                    item_id = video.item_id
                    self.repo.update_variant_text(
                        item_id, "de", de, expected_state=ItemState.DRAFT
                    )
                    self.repo.update_variant_text(
                        item_id, "ua", ua, expected_state=ItemState.DRAFT
                    )
                else:
                    item_id = self._create_item(video, ua=ua, de=de)
                return self.repo.transition(item_id, _AWAITING), warnings
        except IllegalTransition:
            send_callback_result(self.api, chat, message_id, "Already handled")
            return None
        except Exception:  # noqa: BLE001 - roll back the attempt and report only the step
            send_callback_result(
                self.api,
                chat,
                message_id,
                f"Generation failed ({step}), press {retry_button} again.",
            )
            return None

    def _create_item(self, video: OwnVideo, *, ua: str, de: str) -> UUID:
        item_id = self.repo.create_item("own_video").id
        self.repo.add_variant(item_id, "ua", ua)
        self.repo.add_variant(item_id, "de", de)
        self.store.finish(video.id, item_id)
        return item_id

    @staticmethod
    def _check_lengths(de: str, ua: str) -> None:
        if not all(
            text.strip() and len(text) <= LINKEDIN_TEXT_LIMIT for text in (de, ua)
        ):
            raise ValueError("Invalid post length")

    def preview(self, chat: int, item: Item) -> None:
        with self.repo.conn.transaction():
            video = self.store.get(chat, item_id=item.id)
        if video:
            self._preview(video, item)

    def _preview(
        self, video: OwnVideo, item: Item, warnings: tuple[str, ...] = ()
    ) -> None:
        label = "yes" if video.ai_generated else "no"
        details = f"Own video · AI label: {label}"
        if not video.reels_eligible:
            details += f"\nReels skipped: video is {video.duration} s (3-90 s allowed)"
        try:
            self.api.send_video(video.chat_id, video.file_id)
        except TelegramError:
            # Telegram document file IDs can only be reused as documents.
            self.api.send_document_ref(video.chat_id, video.file_id)
        if not video.has_description:
            self.api.send_message(
                video.chat_id,
                f"Own video · no description · Reels only · AI label: {label}",
                approval_keyboard(item.id, edit=False, regenerate=False),
            )
            return
        Notifier(self.api, video.chat_id).send_preview(
            item,
            self.repo.get_variants(item.id),
            source_details="\n".join((details, *warnings)),
            reels=video.reels_eligible,
        )
