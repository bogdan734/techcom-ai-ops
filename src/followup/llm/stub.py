"""Deterministic stand-in for the model, used offline and in tests.

It honours the same contract as a provider: it receives an ``LLMRequest`` and returns JSON
text, which goes through exactly the same parsing and validation as a model's answer.
Notes are classified with keyword rules and messages come from the fallback templates.
"""

from __future__ import annotations

import json
import re

from ..guard import screen_note
from ..templates import render_message
from .base import Completion, LLMError, LLMRequest

_MATERIALS = re.compile(
    r"\b(materials?|warehouse|deliver(y|ies|ed)?|back-?order(ed)?|shipment|supplier|"
    r"vendor|out of stock)\b",
    re.IGNORECASE,
)
_DEPENDENCY = re.compile(
    r"\b(until|upstream|depends on|dependent on|waiting (for|on)|awaiting|pending|"
    r"permit|approval)\b",
    re.IGNORECASE,
)


class StubClient:
    model = "stub"

    def complete(self, request: LLMRequest) -> Completion:
        if request.kind == "read_note":
            answer = read_note(request.facts["note"])
        elif request.kind == "draft_message":
            answer = {"message": render_message(request.facts)}
        else:
            raise LLMError(f"the stub cannot answer {request.kind!r}", fatal=True)
        return Completion(json.dumps(answer))


def read_note(note: str) -> dict[str, object]:
    if _MATERIALS.search(note):
        blocker = "materials"
    elif _DEPENDENCY.search(note):
        blocker = "dependency"
    else:
        blocker = "none"
    return {"blocker": blocker, "contains_instructions": bool(screen_note(note))}
