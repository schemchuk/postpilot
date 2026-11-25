from collections.abc import Callable
from functools import partial
from uuid import UUID

import httpx

from postpilot.channels.base import Channel
from postpilot.channels.linkedin import LINKEDIN_TEXT_CHANNELS, LinkedInChannel
from postpilot.channels.linkedin_video import LinkedInVideoChannel
from postpilot.channels.paused import is_paused
from postpilot.channels.threads import ThreadsChannel
from postpilot.config import Settings


def build_channels(
    settings: Settings, token_reader: Callable[[str], str | None], client: httpx.Client
) -> list[Channel]:
    channels: list[Channel] = []
    if settings.linkedin is not None:
        token_source = partial(token_reader, LinkedInChannel.token_provider)
        channels.extend(
            LinkedInChannel(name, lang, token_source, client, settings.linkedin.version)
            for name, lang in LINKEDIN_TEXT_CHANNELS.items()
            if not is_paused(name)
        )
    if settings.threads is not None:
        token_source = partial(token_reader, ThreadsChannel.token_provider)
        channels.append(ThreadsChannel(token_source, client))
    return channels


def build_own_video_channels(
    channels: list[Channel],
    *,
    fetch_video: Callable[[UUID], bytes],
    ai_generated: Callable[[UUID], bool],
) -> list[Channel]:
    adapters: list[Channel] = []
    for channel in channels:
        if isinstance(channel, LinkedInChannel):
            adapters.append(
                LinkedInVideoChannel(
                    channel, fetch_video=fetch_video, ai_generated=ai_generated
                )
            )
        elif isinstance(channel, ThreadsChannel):
            adapters.append(channel)
    return adapters
