from collections.abc import Callable
from functools import partial

import httpx

from postpilot.channels.base import Channel
from postpilot.channels.linkedin import LinkedInChannel
from postpilot.config import Settings


def build_channels(
    settings: Settings, token_reader: Callable[[str], str | None], client: httpx.Client
) -> list[Channel]:
    if settings.linkedin is None:
        return []
    token_source = partial(token_reader, LinkedInChannel.token_provider)
    return [
        LinkedInChannel(
            "linkedin", "de", token_source, client, settings.linkedin.version
        ),
        LinkedInChannel(
            "linkedin-ua", "ua", token_source, client, settings.linkedin.version
        ),
    ]
