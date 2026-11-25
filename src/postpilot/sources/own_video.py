import json
import logging
import re
from typing import Any
from uuid import UUID

from postpilot.ai.client import TextModel
from postpilot.ai.humanizer import humanize_pair
from postpilot.ai.prompts import OWN_VIDEO_SYSTEM_PROMPT, interview_message
from postpilot.core.limits import LINKEDIN_TEXT_LIMIT
from postpilot.core.models import Item
from postpilot.core.states import IllegalTransition, ItemState
from postpilot.notify.telegram import Notifier, TelegramApi, TelegramError
from postpilot.store.own_videos import OwnVideo, OwnVideoStore
from postpilot.store.repo import ItemRepo

_LOG = logging.getLogger(__name__)

WORK = re.compile(
    r"^/work(?:@[a-zA-Z0-9_]+)?(?:\s+(?P<ai>ai)(?=\s|$))?(?:\s+(?P<text>.*))?$",
    re.DOTALL,
)
_INTERVIEW_LIMIT = 20_000
_FILE_LIMIT = 20_000_000
_AWAITING = ItemState.AWAITING_APPROVAL
_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"ua": {"type": "string"}, "de": {"type": "string"}},
    "required": ["ua", "de"],
    "additionalProperties": False,
}


class OwnVideoInbox:
    def __init__(
        self, repo: ItemRepo, api: TelegramApi, model: TextModel | None
    ) -> None:
        self.repo, self.api, self.model = repo, api, model
        self.store = OwnVideoStore(repo.conn)

    def message(self, update_id: int, chat: int, message: dict[str, Any]) -> bool:
        text = message.get("text", "")
        doc = message.get("document") or {}
        caption = WORK.fullmatch(message.get("caption", ""))
        media = message.get("video") or (
            doc if doc.get("mime_type") == "video/mp4" else None
        )
        reply_id = (message.get("reply_to_message") or {}).get("message_id")
        command = caption or WORK.fullmatch(text)
        if command is None and type(reply_id) is not int:
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
                        generated = self._generate(video)
            elif command:
                if self.store.claim_update(update_id):
                    self.api.send_message(
                        chat, "Send a video with caption: /work [ai] [interview text]"
                    )
            elif type(reply_id) is int and (
                video := self.store.get(chat, prompt_message_id=reply_id)
            ):
                if video.status == "collecting" and self.store.claim_update(update_id):
                    self._append(video, text, doc)
            else:
                return False
        if generated and video:
            self._preview(video, *generated)
        return True

    def _add(
        self, chat: int, media: dict[str, Any], ai: bool, text: str
    ) -> OwnVideo | None:
        if (media.get("file_size") or 0) > _FILE_LIMIT:
            self.api.send_message(
                chat, "Video over 20 MB: send it as video, not as file, or shorten it."
            )
            return None
        id = self.store.add(chat, media, ai, text)
        if id is None:
            self.api.send_message(chat, "This video was already added.")
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

    def _append(self, video: OwnVideo, text: str, doc: dict[str, Any]) -> None:
        chat = video.chat_id
        if doc.get("mime_type") == "text/plain" or doc.get(
            "file_name", ""
        ).lower().endswith(".txt"):
            try:
                text = self.api.download_file(self.api.get_file(doc["file_id"])).decode(
                    "utf-8-sig"
                )
            except (TelegramError, UnicodeError):
                self.api.send_message(
                    chat, "Could not read the file. Send a UTF-8 .txt file."
                )
                return
        if not text.strip():
            return
        joined = "\n\n".join(part for part in (video.interview, text.strip()) if part)
        if len(joined) > _INTERVIEW_LIMIT:
            self.api.send_message(chat, "Interview exceeds 20,000 characters.")
            return
        self.store.set_interview(video.id, joined)
        self.api.send_message(chat, f"Text added ({len(joined)} chars).")

    def callback(
        self,
        update_id: int,
        chat: int,
        message_id: object,
        callback_id: str,
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
                self._answer(callback_id, "Already handled")
                return
            self._answer(callback_id)
            if action == "own:skip":
                self.store.finish(video.id)
                if type(message_id) is int:
                    self.api.edit_message_text(chat, message_id, "Skipped")
                return
            generated = (
                self._generate(video) if regen or video.item_id is None else None
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
            finally:
                self._preview(video, preview, warnings)

    def _answer(self, callback_id: str, text: str | None = None) -> None:
        # Polling every few minutes means the callback query may already have
        # expired; the button press itself must still be handled.
        try:
            if text is None:
                self.api.answer_callback_query(callback_id)
            else:
                self.api.answer_callback_query(callback_id, text)
        except TelegramError:
            _LOG.warning("Telegram callback reply failed")

    def _generate(self, video: OwnVideo) -> tuple[Item, tuple[str, ...]] | None:
        chat = video.chat_id
        if not video.interview:
            self.api.send_message(chat, "Add the interview text first.")
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
                    item_id = self.repo.create_item("own_video").id
                    self.repo.add_variant(item_id, "ua", ua)
                    self.repo.add_variant(item_id, "de", de)
                    self.store.finish(video.id, item_id)
                return self.repo.transition(item_id, _AWAITING), warnings
        except IllegalTransition:
            self.api.send_message(chat, "Already handled")
            return None
        except Exception:  # noqa: BLE001 - roll back the attempt and report only the step
            self.api.send_message(
                chat, f"Generation failed ({step}), press Generate again."
            )
            return None

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
        Notifier(self.api, video.chat_id).send_preview(
            item,
            self.repo.get_variants(item.id),
            source_details="\n".join((details, *warnings)),
            reels=video.reels_eligible,
        )
