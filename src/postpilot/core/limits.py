import re

LINKEDIN_TEXT_LIMIT = 3000
REELS_MIN_DURATION = 3
REELS_MAX_DURATION = 90

_SENTENCE_END = re.compile(r"""[.!?…]+["'”’»“‘)\]]*(?=\s|$)""")


def shorten_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    budget = limit - 1
    sentence_end = max(
        (m.end() for m in _SENTENCE_END.finditer(text) if m.end() <= budget),
        default=0,
    )
    word_end = max(
        (m.end() for m in re.finditer(r"\S+(?=\s)", text) if m.end() <= budget),
        default=budget,
    )
    return text[: sentence_end or word_end] + "…"
