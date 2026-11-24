from collections.abc import Callable
from functools import partial
from uuid import UUID

import httpx

from postpilot.channels.base import Channel
from postpilot.channels.linkedin import LinkedInChannel
from postpilot.channels.linkedin_video import LinkedInVideoChannel
from postpilot.channels.threads import ThreadsChannel
from postpilot.config import Settings


def build_channels(
    settings: Settings, token_reader: Callable[[str], str | None], client: httpx.Client
) -> list[Channel]:
    channels: list[Channel] = []
    if settings.linkedin is not None:
        token_source = partial(token_reader, LinkedInChannel.token_provider)
        channels.extend(
            [
                LinkedInChannel(
                    "linkedin", "de", token_source, client, settings.linkedin.version
                ),
                LinkedInChannel(
                    "linkedin-ua", "ua", token_source, client, settings.linkedin.version
                ),
            ]
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
    return [
        LinkedInVideoChannel(
            channel, fetch_video=fetch_video, ai_generated=ai_generated
        )
        for channel in channels
        if isinstance(channel, LinkedInChannel)
    ]
