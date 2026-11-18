from uuid import UUID, uuid4

from postpilot.channels.base import ChannelError, TransientChannelError, marker
from postpilot.core.models import Item, Variant


class SimulatedCrash(BaseException):
    pass


class FakeChannel:
    def __init__(
        self, name: str, required_lang: str = "de", max_len: int | None = None
    ) -> None:
        self.name = name
        self.required_lang = required_lang
        self.max_len = max_len
        self.posts: dict[str, str] = {}
        self.fail_times = 0
        self.fail_permanently = False
        self.crash_after_send = False

    def _text(self, item: Item, variants: list[Variant]) -> str:
        for variant in variants:
            if variant.item_id == item.id and variant.lang == self.required_lang:
                return variant.text
        raise ChannelError("Missing required language variant")

    def validate(self, item: Item, variants: list[Variant]) -> None:
        text = self._text(item, variants)
        if self.max_len is not None and len(text) > self.max_len:
            raise ChannelError("Text exceeds the channel length limit")

    def publish(self, item: Item, variants: list[Variant]) -> str:
        self.validate(item, variants)
        if self.fail_permanently:
            raise ChannelError("Permanent fake channel failure")
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TransientChannelError("Transient fake channel failure")
        remote_id = str(uuid4())
        self.posts[remote_id] = f"{self._text(item, variants)}\n\n{marker(item.id)}"
        if self.crash_after_send:
            raise SimulatedCrash
        return remote_id

    def find_existing(self, item_id: UUID) -> str | None:
        for remote_id, text in self.posts.items():
            if text.endswith(f"\n\n{marker(item_id)}"):
                return remote_id
        return None
