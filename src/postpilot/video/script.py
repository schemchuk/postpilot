import re

from postpilot.ai.client import AIError, TextModel
from postpilot.ai.prompts import SCRIPT_SYSTEM_PROMPT

_FORBIDDEN = re.compile(
    r"://|\bwww\.|\bmailto:|\b[\w-]+\.(?:de|com|org|net|io|dev|eu|ua)\b"
    r"|[#\ufe0f\u20e3\U0001f000-\U0001faff\u2600-\u27bf\u00a9\u00ae\u2122]",
    re.IGNORECASE,
)


class ScriptError(RuntimeError):
    """The model did not produce a usable spoken script."""


def generate_script(model: TextModel, text: str) -> str:
    try:
        script = model.complete(SCRIPT_SYSTEM_PROMPT, text).strip()
    except AIError:
        raise ScriptError("Script generation failed") from None
    if not 50 <= len(script.split()) <= 140 or _FORBIDDEN.search(script):
        raise ScriptError(
            "Script must contain 50–140 words without URLs, hashtags or emoji"
        )
    return script
