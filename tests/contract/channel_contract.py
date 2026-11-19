from abc import ABC, abstractmethod
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from postpilot.channels.base import ChannelError, MarkerChannel, marker
from postpilot.core.models import Item, Variant
from postpilot.core.states import ItemState


class ChannelContract(ABC):
    @pytest.fixture
    @abstractmethod
    def make_channel(self) -> MarkerChannel:
        """Supply a fresh adapter backed by inspectable remote storage."""
        ...

    @abstractmethod
    def read_post(self, channel: MarkerChannel, remote_id: str) -> str:
        """Read the stored remote post for the marker assertion."""
        ...

    @pytest.fixture
    def item(self) -> Item:
        now = datetime.now(UTC)
        return Item(uuid4(), "github", ItemState.APPROVED, now, now, 1, now)

    def test_missing_variant(self, make_channel: MarkerChannel, item: Item) -> None:
        with pytest.raises(ChannelError):
            make_channel.validate(item, [])

    def test_publish_and_lookup(self, make_channel: MarkerChannel, item: Item) -> None:
        variants = [
            Variant(item.id, "de", "Ein Beitrag"),
            Variant(item.id, "ua", "Допис"),
        ]
        make_channel.validate(item, variants)
        remote_id = make_channel.publish(item, variants)
        assert isinstance(remote_id, str) and remote_id
        assert make_channel.find_existing(item.id) == remote_id
        assert make_channel.find_existing(uuid4()) is None
        assert marker(item.id) in self.read_post(make_channel, remote_id)
