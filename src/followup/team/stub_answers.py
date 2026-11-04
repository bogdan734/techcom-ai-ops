"""What the offline stub says when the team layer asks it a question, built from the rules.

It lets the whole chain (pins, grounding, audit, second opinion) run without a key. The stub
grades a little more loosely than the rules, accepting a word that shares its first five letters
with a key term, so a near miss shows up as a rule and model split and goes to review.
"""

from __future__ import annotations

import re

from ..llm.base import LLMError, LLMRequest
from .assistant import retrieve
from .grading import _stem
from .spec_terms import identifiers, key_terms, requirement_sentences

TEAM_KINDS = frozenset(
    {"quiz_questions", "spec_tokens", "grade_answer", "audit_grade", "assist_answer"}
)
_PASS = 0.6
_PREFIX = 5
_WORD = re.compile(r"\d+(?:[.,]\d+)?|[A-Za-z][A-Za-z0-9_-]*")


def answer(request: LLMRequest) -> dict[str, object]:
    facts = request.facts
    if request.kind == "quiz_questions":
        return {"questions": _questions(facts["spec"])}
    if request.kind == "spec_tokens":
        return {"tokens": identifiers(facts["spec"])[:20]}
    if request.kind in ("grade_answer", "audit_grade"):
        return _grade(facts["answer"], facts["key_terms"])
    if request.kind == "assist_answer":
        return {"quotes": list(retrieve(facts["spec"], facts["question"]))}
    raise LLMError(f"the stub cannot answer {request.kind!r}", fatal=True)


def _questions(spec: str) -> list[dict[str, object]]:
    items = []
    for sentence in requirement_sentences(spec)[:6]:
        terms = key_terms(sentence, limit=3)
        if terms:
            topic = max(terms, key=len)
            items.append(
                {
                    "prompt": f"What does the task say about {topic}?",
                    "quote": sentence,
                    "key_terms": list(terms),
                }
            )
    return items


def _grade(text: str, terms: list[str]) -> dict[str, object]:
    matched = [(term, _find(text, term)) for term in terms]
    hits = [word for _, word in matched if word]
    correct = bool(terms) and len(hits) / len(terms) >= _PASS
    return {"correct": correct, "evidence": hits[0] if correct else ""}


def _find(text: str, term: str) -> str | None:
    """The word of ``text`` that matches ``term`` exactly (as a stem) or by its first letters."""
    stem = _stem(term)
    for match in _WORD.finditer(text):
        word = _stem(match.group(0).lower())
        if word == stem or (len(stem) >= _PREFIX and word[:_PREFIX] == stem[:_PREFIX]):
            return match.group(0)
    return None
