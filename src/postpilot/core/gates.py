from collections.abc import Mapping

from postpilot.ai.client import TextModel
from postpilot.ai.humanizer import humanize_post


def humanizer_gate(
    model: TextModel, text: str, lang: str, facts: Mapping[str, object] | None = None
) -> str:
    """Run on S1 only. Never run on S2 text (F2.2).

    A failing text must never reach the queue. The caller must not create an item on error.
    """
    return humanize_post(model, text, lang, facts)
