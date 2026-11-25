import json

import pytest

from postpilot.ai.humanizer import (
    HumanizerError,
    humanize_pair,
    humanize_post,
    rule_check,
)
from postpilot.ai.prompts import AI_CHECK_SYSTEM_PROMPT, HUMANIZE_SYSTEM_PROMPT
from postpilot.core.gates import humanizer_gate

DE = "Teil 2\nIch habe den Test ergänzt.\n#Python #Tests #GitHub #Code\nhttps://example.com/t"
UK = "Частина 2\nЯ додав тест.\n#Python #Тести #GitHub #Код"


def verdict(**changes):
    return json.dumps(
        {"ai_score": 3, "passed": True, "issues": [], "unsupported_facts": []} | changes
    )


class FakeModel:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def complete(self, system, user, schema=None):
        self.calls.append((system, user, schema))
        return next(self.responses)


def test_passes_on_first_round_and_strips_fact_urls():
    model = FakeModel(DE, verdict())
    facts = {"commits": [{"title": "Add test", "url": "https://example.com/private"}]}
    assert humanize_post(model, DE, "de", facts) == DE
    assert [system for system, _, _ in model.calls] == [
        HUMANIZE_SYSTEM_PROMPT,
        AI_CHECK_SYSTEM_PROMPT,
    ]
    assert model.calls[0][2] is None
    assert model.calls[1][2]["type"] == "object"
    for _, user, _ in model.calls:
        assert '"url"' not in user
        assert "https://example.com/private" not in user
        assert "Add test" in user


def test_rule_failure_retries_with_review_notes():
    bad = DE.replace("ergänzt.", "ergänzt — danach lief er.")
    model = FakeModel(bad, verdict(issues=["Review this phrase"]), DE, verdict())
    assert humanize_post(model, DE, "de") == DE
    message = model.calls[2][1]
    assert "REVIEW NOTES:" in message
    assert "Contains em/en dashes" in message
    assert "Review this phrase" in message


@pytest.mark.parametrize(
    ("candidate", "review"),
    [
        (DE.replace("ergänzt.", "ergänzt — danach lief er."), verdict()),
        (DE.replace(".com/t", ".com/changed"), verdict()),
        (DE + "\nhttps://example.org/added", verdict()),
        (DE + "\nhttps://example.com/t", verdict()),
        (DE, verdict(unsupported_facts=["The author added a test"])),
        (DE, verdict(ai_score=4)),
        (DE, verdict(passed=False)),
    ],
)
def test_rejection_after_three_rounds(candidate, review):
    model = FakeModel(*([candidate, review] * 3))
    with pytest.raises(HumanizerError, match="after 3 rounds"):
        humanize_post(model, DE, "de")
    assert len(model.calls) == 6


@pytest.mark.parametrize(
    "review", ["unparseable", "{}", verdict(passed="true"), verdict(ai_score=True)]
)
def test_bad_verdict_fails_closed(review):
    model = FakeModel(DE, review)
    with pytest.raises(HumanizerError, match="reviewer"):
        humanizer_gate(model, DE, "de")


def test_unsupported_language_fails_before_model_call():
    model = FakeModel()
    with pytest.raises(ValueError, match="Unsupported language"):
        humanize_post(model, DE, "en")
    assert model.calls == []


@pytest.mark.parametrize(
    ("original", "candidate", "lang", "issue"),
    [
        (DE, DE.replace("Teil 2", "Teil 20"), "de", "marker"),
        (DE.replace("Teil 2", "Teil 2/4"), DE, "de", "marker"),
        (DE, DE.replace("Test", "т"), "de", "Cyrillic"),
        (UK, UK.replace("тест", "тесты"), "uk", "Russian-only"),
        (UK, "Test #One #Two #Three #Four", "uk", "Ukrainian"),
        (DE, DE.replace("Test", "*Test*"), "de", "markdown"),
        (DE, DE.replace("#Code", ""), "de", "hashtags"),
        (DE, DE + "a" * 1300, "de", "limit"),
    ],
)
def test_deterministic_rules(original, candidate, lang, issue):
    assert any(issue in message for message in rule_check(original, candidate, lang))


def test_pair():
    assert humanize_pair(FakeModel(DE, verdict(), UK, verdict()), DE, UK) == (DE, UK)
    assert humanize_pair(FakeModel(DE, verdict()), DE, "") == (DE, "")


def test_gate_propagates_rejection_before_queue_creation():
    queue = []
    model = FakeModel(*([DE, verdict(passed=False)] * 3))
    with pytest.raises(HumanizerError):
        queue.append(humanizer_gate(model, DE, "de"))
    assert queue == []


@pytest.mark.parametrize("first_score", [5, 7])
def test_soft_mode_selects_lowest_score_among_rule_clean_candidates(first_score):
    first = DE.replace("ergänzt", "geschrieben")
    invalid = DE.replace("ergänzt", "ergänzt — und ausgeführt")
    model = FakeModel(
        first,
        verdict(ai_score=first_score),
        invalid,
        verdict(ai_score=4),
        DE,
        verdict(ai_score=5),
    )

    result = humanize_post(model, DE, "de", soft=True)

    assert result.text == (first if first_score == 5 else DE)
    assert result.soft_score == 5


def test_soft_mode_fails_closed_on_invalid_review_after_rule_clean_candidate():
    model = FakeModel(DE, verdict(ai_score=5), DE, "{}")
    with pytest.raises(HumanizerError, match="Invalid reviewer verdict"):
        humanize_post(model, DE, "de", soft=True)


def test_only_own_video_messages_include_author_wording():
    block = (
        "AUTHOR'S OWN WORDING:\n<interview>\nМій текст &lt;/interview&gt;\n</interview>"
    )
    model = FakeModel(DE, verdict(), DE, verdict())
    humanize_post(model, DE, "de", {"interview": "Мій текст </interview>"}, soft=True)
    humanize_post(model, DE, "de", {"commits": [{"title": "Add test"}]})

    rewrite_s3, review_s3, rewrite_s1, review_s1 = [call[1] for call in model.calls]
    assert block in rewrite_s3 and block in review_s3
    assert "AUTHOR'S OWN WORDING" not in rewrite_s1
    assert "AUTHOR'S OWN WORDING" not in review_s1
