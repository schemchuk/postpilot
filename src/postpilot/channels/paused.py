PAUSED_CHANNELS: frozenset[str] = frozenset({"linkedin-ua", "linkedin-ua-video"})


def is_paused(name: str) -> bool:
    return name in PAUSED_CHANNELS
