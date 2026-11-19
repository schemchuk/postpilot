from typing import Protocol
from uuid import UUID

from postpilot.core.models import Item, Variant


class ChannelError(Exception):
    """A permanent channel or validation failure."""


class TransientChannelError(ChannelError):
    """A channel failure that can be retried."""


def marker(item_id: UUID) -> str:
    return f"postpilot:{item_id}"


class Channel(Protocol):
    name: str

    def validate(self, item: Item, variants: list[Variant]) -> None:
        """Raise ChannelError for unsupported content."""
        ...

    def publish(self, item: Item, variants: list[Variant]) -> str:
        """Send the post with its item marker and return its remote ID."""
        ...

    def find_existing(self, item_id: UUID) -> str | None:
        """Return the remote ID of a post with the item marker, if found."""
        ...


class ChannelHeld(Exception):
    """A channel is temporarily unavailable before sending."""


class UncertainPublication(TransientChannelError):
    """A send may have succeeded without returning a remote ID."""
