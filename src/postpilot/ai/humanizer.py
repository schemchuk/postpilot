import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypedDict, overload

from .client import AIError, TextModel
from .prompts import (
    AI_CHECK_SYSTEM_PROMPT,
    HUMANIZE_SYSTEM_PROMPT,
    review_message,
    rewrite_message,
)

MAX_ROUNDS = 3
MAX_CHARS = 1300
MAX_AI_SCORE = 3
LANG_NAMES = {"de": "German", "uk": "Ukrainian"}

_VERDICT_FIELDS = ["ai_score", "passed", "issues", "unsupported_facts"]
_CHECK_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "ai_score": {"type": "integer"},
        "passed": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "unsupported_facts": {"type": "array", "items": {"type": "string"}},
    },
    "required": _VERDICT_FIELDS,
    "additionalProperties": False,
}
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\[\]]+", re.IGNORECASE)
_MARKER = re.compile(r"^\s*(Teil|Частина)\s+(\d+(?:/\d+)?)(?![\w/])")
_CYRILLIC = re.compile(r"[\u0400-\u052f\u1c80-\u1c8f\u2de0-\u2dff\ua640-\ua69f]")
_MARKDOWN = re.compile(
    r"`|\*\*|__|~~|(?<!\w)(?:\*[^*\n]+\*|_[^_\n]+_)"
    r"|!?\[[^\]\n]+\]\(|<\/?[a-zA-Z][^>]*>"
    r"|^\s*(?:#{1,6}\s|>\s?|[-+*]\s|\d+[.)]\s|[-*_]{3,}\s*$|~~~)",
    re.MULTILINE,
)


class HumanizerError(RuntimeError):
    """A draft could not be verified."""


class Verdict(TypedDict):
    ai_score: int
    passed: bool
    issues: list[str]
    unsupported_facts: list[str]


def _language_name(lang: str) -> str:
    if lang not in LANG_NAMES:
        raise ValueError(f"Unsupported language: {lang}")
    return LANG_NAMES[lang]


def rule_check(original: str, text: str, lang: str) -> list[str]:
    """Check the candidate against language, formatting and preservation rules."""
    _language_name(lang)
    issues = []
    if len(text) > MAX_CHARS:
        issues.append(f"Post is {len(text)} chars, limit is {MAX_CHARS}. Shorten it.")
    prose = _URL.sub("", text)
    has_cyrillic = bool(_CYRILLIC.search(text))
    if lang == "de" and has_cyrillic:
        issues.append("Post must be written in German and contains Cyrillic text.")
    if lang == "uk" and (not has_cyrillic or re.search(r"[ыэъЫЭЪ]", text)):
        issues.append(
            "Post must be written in Ukrainian without Russian-only letters (ы, э, ъ)."
        )
    if "—" in text or "–" in text:
        issues.append("Contains em/en dashes. Replace them with other punctuation.")
    if _MARKDOWN.search(prose):
        issues.append("Contains markdown or HTML. Use plain text.")
    if Counter(_URL.findall(original)) != Counter(_URL.findall(text)):
        issues.append(
            "URLs must match the original draft exactly, including their count."
        )
    marker = _MARKER.match(original)
    rewritten_marker = _MARKER.match(text)
    if marker and (
        not rewritten_marker or rewritten_marker.groups() != marker.groups()
    ):
        issues.append(f"Keep the series marker at the start: {marker.group(0).strip()}")
    hashtags = re.findall(r"(?<!\S)#\w+", prose)
    if not 4 <= len(hashtags) <= 6:
        issues.append(f"Needs 4-6 hashtags, found {len(hashtags)}.")
    return issues


def ai_check(
    model: TextModel, text: str, lang: str, facts: Mapping[str, object] | None = None
) -> Verdict:
    """Validate the reviewer response before using it as an approval decision."""
    user = review_message(_language_name(lang), text, facts)
    try:
        raw = model.complete(AI_CHECK_SYSTEM_PROMPT, user, _CHECK_SCHEMA)
    except AIError:
        raise HumanizerError("The reviewer request failed.") from None
    try:
        verdict = json.loads(raw)
    except (ValueError, TypeError):
        raise HumanizerError("Unparseable reviewer response.") from None
    if not isinstance(verdict, dict) or verdict.keys() != set(_VERDICT_FIELDS):
        raise HumanizerError("Invalid reviewer verdict.")
    score, passed = verdict["ai_score"], verdict["passed"]
    issues, unsupported = verdict["issues"], verdict["unsupported_facts"]
    if type(score) is not int or not 0 <= score <= 10 or type(passed) is not bool:
        raise HumanizerError("Invalid reviewer verdict.")
    if not all(
        isinstance(values, list) and all(isinstance(i, str) for i in values)
        for values in (issues, unsupported)
    ):
        raise HumanizerError("Invalid reviewer verdict.")
    return {
        "ai_score": score,
        "passed": passed and score <= MAX_AI_SCORE and not unsupported,
        "issues": issues,
        "unsupported_facts": unsupported,
    }


@dataclass(frozen=True)
class HumanizedPost:
    text: str
    soft_score: int | None = None


@overload
def humanize_post(
    model: TextModel,
    text: str,
    lang: str,
    facts: Mapping[str, object] | None = None,
    *,
    soft: Literal[False] = False,
) -> str: ...


@overload
def humanize_post(
    model: TextModel,
    text: str,
    lang: str,
    facts: Mapping[str, object] | None = None,
    *,
    soft: Literal[True],
) -> HumanizedPost: ...


def humanize_post(
    model: TextModel,
    text: str,
    lang: str,
    facts: Mapping[str, object] | None = None,
    *,
    soft: bool = False,
) -> str | HumanizedPost:
    """Rewrite a draft and verify it, allowing AI review failures only in soft mode."""
    language = _language_name(lang)
    candidate = text
    notes: list[str] = []
    best: tuple[int, str] | None = None
    for _ in range(MAX_ROUNDS):
        user = rewrite_message(language, candidate, facts, notes)
        try:
            candidate = model.complete(HUMANIZE_SYSTEM_PROMPT, user).strip()
        except AIError:
            raise HumanizerError("The rewrite request failed.") from None
        notes = rule_check(text, candidate, lang)
        verdict = ai_check(model, candidate, lang, facts)
        if not notes:
            if verdict["passed"]:
                return HumanizedPost(candidate) if soft else candidate
            score = verdict["ai_score"]
            if soft and (best is None or score < best[0]):
                best = score, candidate
        notes.extend(verdict["issues"])
        notes.extend(
            f"Unsupported by source facts, remove or soften: {fact}"
            for fact in verdict["unsupported_facts"]
        )
        if not notes:
            notes.append("The reviewer rejected the draft. Reduce AI tells.")

    if best is not None:
        score, candidate = best
        return HumanizedPost(candidate, score)

    raise HumanizerError(
        f"{language} post failed the humanizer after {MAX_ROUNDS} rounds."
    )


@overload
def humanize_pair(
    model: TextModel,
    de: str,
    uk: str,
    facts: Mapping[str, object] | None = None,
    *,
    soft: Literal[False] = False,
) -> tuple[str, str]: ...


@overload
def humanize_pair(
    model: TextModel,
    de: str,
    uk: str,
    facts: Mapping[str, object] | None = None,
    *,
    soft: Literal[True],
) -> tuple[HumanizedPost, HumanizedPost]: ...


def humanize_pair(
    model: TextModel,
    de: str,
    uk: str,
    facts: Mapping[str, object] | None = None,
    *,
    soft: bool = False,
) -> tuple[str, str] | tuple[HumanizedPost, HumanizedPost]:
    """Verify German and optional Ukrainian variants."""
    if soft:
        return humanize_post(model, de, "de", facts, soft=True), (
            humanize_post(model, uk, "uk", facts, soft=True)
            if uk
            else HumanizedPost(uk)
        )
    return humanize_post(model, de, "de", facts), (
        humanize_post(model, uk, "uk", facts) if uk else uk
    )
