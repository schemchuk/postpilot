from collections.abc import Callable
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from postpilot.core.models import Item, Variant


class ChannelError(Exception):
    """A permanent channel or validation failure."""


class ConfirmedPublicationFailure(ChannelError):
    """A failure after establishing that no post was published."""


class TransientChannelError(ChannelError):
    """A channel failure that can be retried."""


def marker(item_id: UUID) -> str:
    return f"postpilot:{item_id}"


class _Channel(Protocol):
    name: str

    def validate(self, item: Item, variants: list[Variant]) -> None:
        """Raise ChannelError for unsupported content."""
        ...


class MarkerChannel(_Channel, Protocol):
    def publish(self, item: Item, variants: list[Variant]) -> str:
        """Send the post with its item marker and return its remote ID."""
        ...

    def find_existing(self, item_id: UUID) -> str | None:
        """Return the remote ID of a post with the item marker, if found."""
        ...


@runtime_checkable
class RefChannel(_Channel, Protocol):
    def publish_with_ref(
        self,
        item: Item,
        variants: list[Variant],
        *,
        ref: str | None,
        claimed_at: datetime,
        save_ref: Callable[[str], None],
    ) -> str | None:
        """Persist the container reference before sending, or resume an existing one."""
        ...


type Channel = MarkerChannel | RefChannel


class ChannelSkipped(Exception):
    """An unsupported item is intentionally omitted from this channel."""


class ChannelHeld(Exception):
    """A channel is temporarily unavailable before sending."""


class UncertainPublication(TransientChannelError):
    """A send may have succeeded without returning a remote ID."""
