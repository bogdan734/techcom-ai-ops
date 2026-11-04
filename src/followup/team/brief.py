"""A task spec laid out for the person who reads it, and a list of what the author left out.

Rules only: it reorders and flags the author's own words and writes nothing of its own, so a
requirement in the brief is always a sentence the issuer wrote. The gaps are questions to the
author, asked before the task goes out, because a check cannot be fair about what the spec never
said.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from .spec_terms import identifiers, limits, requirement_sentences, sentences

_VAGUE = re.compile(
    r"\b(asap|as soon as possible|soon|quickly|as needed|etc|and so on|"
    r"good|nice|properly|whenever)\b",
    re.IGNORECASE,
)
_LONG = 40
_SHORT = 12
_NUMBER = re.compile(r"\d")


@dataclass(frozen=True)
class Brief:
    goal: str
    requirements: tuple[str, ...]
    limits: tuple[str, ...]
    names: tuple[str, ...]
    other: tuple[str, ...]
    gaps: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def structure(spec: str, assignee_role: str | None = None, due: str | None = None) -> Brief:
    parts = sentences(spec)
    goal = parts[0] if parts else ""
    requirements = tuple(s for s in requirement_sentences(spec) if s != goal)
    placed = {goal, *requirements}
    return Brief(
        goal=goal,
        requirements=requirements,
        limits=tuple(limits(spec)),
        names=tuple(identifiers(spec)),
        other=tuple(s for s in parts if s not in placed),
        gaps=tuple(_gaps(spec, parts, assignee_role, due)),
    )


def _gaps(spec: str, parts: list[str], role: str | None, due: str | None) -> list[str]:
    found: list[str] = []
    words = len(spec.split())
    if words < _SHORT:
        found.append("Say what has to be done and how it will be accepted: the text is very short.")
    if not requirement_sentences(spec):
        found.append("Write at least one requirement with 'must', 'shall' or 'should'.")
    if not _NUMBER.search(spec):
        found.append("No measurable limit found: add a number, a unit or a deadline.")
    for match in dict.fromkeys(m.group(0).lower() for m in _VAGUE.finditer(spec)):
        found.append(f"Replace '{match}' with a date, a number or a named outcome.")
    if due is not None and not due:
        found.append("No due date is set.")
    if role == "engineer" and not identifiers(spec):
        found.append(
            "Name the commands, options or files that must exist: they are checked in the code."
        )
    for sentence in parts:
        if len(sentence.split()) > _LONG:
            found.append(f"Split a long sentence in two: '{sentence[:48]}...'")
    return found
