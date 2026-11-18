import pytest
from channel_contract import ChannelContract

from postpilot.channels.base import Channel
from postpilot.channels.fake import FakeChannel


class TestFakeChannel(ChannelContract):
    @pytest.fixture
    def make_channel(self) -> FakeChannel:
        return FakeChannel("fake")

    def read_post(self, channel: Channel, remote_id: str) -> str:
        assert isinstance(channel, FakeChannel)
        return channel.posts[remote_id]
