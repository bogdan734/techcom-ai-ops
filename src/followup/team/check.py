"""The layer that checks the model: is what it wrote in the source, and does it keep being right.

Three checks, none of which trusts the model:
- grounding: an item survives only if its quote appears verbatim in the source text;
- a second opinion from another model or prompt on a hash-seeded sample, always while the model
  is on tightened inspection;
- a ledger of how often each feature of each model passes these checks. A feature whose record
  turns bad falls back to rules and the issuers are told.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import date
from typing import TYPE_CHECKING, TypeVar

from .sampling import sampled
from .spec_terms import contains_verbatim
from .trust import Event, Ledger, Level, decayed_rate, standing

if TYPE_CHECKING:
    from .settings import TeamSettings

T = TypeVar("T")
_SAME_ENOUGH = 0.5


def keep_grounded(
    items: Sequence[T], source: str, quote: Callable[[T], str]
) -> tuple[list[T], int]:
    """The items whose quote is in ``source`` verbatim, and how many were dropped."""
    kept = [item for item in items if contains_verbatim(source, quote(item))]
    return kept, len(items) - len(kept)


def consistent(first: Iterable[str], second: Iterable[str]) -> bool:
    """Two answers to the same prompt agree when their sets overlap by half (Jaccard)."""
    a, b = set(first), set(second)
    if not a and not b:
        return True
    return len(a & b) / len(a | b) >= _SAME_ENOUGH


class ModelAudit:
    """One model's record per feature (``quiz_questions``, ``grade_answer``, ...)."""

    def __init__(self, ledger: Ledger, model: str, rules: TeamSettings, today: date) -> None:
        self.ledger = ledger
        self.model = model
        self.rules = rules
        self.today = today

    def subject(self, feature: str) -> str:
        return f"{self.model}:{feature}"

    def record(self, feature: str, item: str, kind: str, passed: bool) -> bool:
        return self.ledger.record(Event(self.subject(feature), item, kind, passed, self.today))

    def level(self, feature: str) -> Level:
        events = self.ledger.events(self.subject(feature))
        return standing(events, self.today, self.rules).level

    def rules_only(self, feature: str) -> bool:
        """True once the feature has a record and the record is bad.

        A new model is not penalised for having no record yet: it needs ``probation`` results
        before its pass rate can drop it below ``model_floor``.
        """
        events = self.ledger.events(self.subject(feature))
        if len(events) < self.rules.trust_probation_tasks:
            return False
        if standing(events, self.today, self.rules).level is Level.HALTED:
            return True
        return (
            decayed_rate(events, self.today, self.rules.trust_half_life_days)
            < self.rules.model_floor
        )

    def second_opinion(self, feature: str, item: str) -> bool:
        if self.level(feature) in (Level.TIGHTENED, Level.HALTED):
            return True
        return sampled(f"{self.subject(feature)}|{item}", self.rules.audit_rate)
