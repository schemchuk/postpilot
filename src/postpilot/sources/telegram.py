from postpilot.ai.client import AIError, TextModel
from postpilot.ai.prompts import TRANSLATE_SYSTEM_PROMPT
from postpilot.core.limits import LINKEDIN_TEXT_LIMIT
from postpilot.notify.telegram import Notifier, TelegramApi
from postpilot.store.repo import ItemRepo


def translate_to_german(model: TextModel, text: str) -> str:
    translation = model.complete(TRANSLATE_SYSTEM_PROMPT, text)
    if not translation.strip():
        raise AIError("Translation returned no text.")
    return translation


class TelegramInbox:
    def __init__(
        self, repo: ItemRepo, api: TelegramApi, model: TextModel | None
    ) -> None:
        self.repo = repo
        self.api = api
        self.model = model

    def handle_text(self, update_id: int, chat_id: int, text: str) -> None:
        if (
            not text.strip()
            or text.startswith("/")
            or self.repo.has_inbox_update(update_id)
        ):
            return
        if len(text) > LINKEDIN_TEXT_LIMIT:
            self.api.send_message(
                chat_id, f"Original exceeds the {LINKEDIN_TEXT_LIMIT}-character limit."
            )
            return
        if self.model is None:
            self.api.send_message(chat_id, "Translation is not configured.")
            return
        try:
            translation = translate_to_german(self.model, text)
        except AIError:
            self.api.send_message(
                chat_id, "Translation failed. Please resend the text."
            )
            return
        if len(translation) > LINKEDIN_TEXT_LIMIT:
            self.api.send_message(
                chat_id,
                f"Translation exceeds the {LINKEDIN_TEXT_LIMIT}-character limit.",
            )
            return

        item = self.repo.create_inbox_item(update_id, text, translation)
        if item is not None:
            Notifier(self.api, chat_id).send_preview(
                item, self.repo.get_variants(item.id)
            )
