"""Trust as a ledger of results, scored when read, never as a number that is updated.

Why a ledger: an event has a unique key, so a rerun cannot count a result twice, and a level can
be explained by listing the events behind it. The score is the lower bound of the Wilson
interval over exponentially decayed results, so a short record scores low however clean it is
and an old record fades. The levels borrow the switching rules of acceptance sampling
(ISO 2859-1): normal, tightened after repeated failures, reduced after a long clean run. The
thresholds are settings of this repository, not the standard's.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from .sampling import sampled

if TYPE_CHECKING:
    from .settings import TeamSettings

# An issuer's manual clearance after a halt; it is not a result and is not scored.
REVIEW_CLEARED = "review_cleared"
_WINDOW = 5  # results looked at for "two failures in five"
_CLEAN_RUN = 5  # passes in a row that end tightened inspection
_NORMAL_QUESTIONS = 3
_NORMAL_TOKENS = 5
_REDUCED_TOKENS = 2


class Level(StrEnum):
    TIGHTENED = "tightened"
    NORMAL = "normal"
    REDUCED = "reduced"
    HALTED = "halted"


@dataclass(frozen=True)
class Event:
    subject: str
    item: str
    kind: str
    passed: bool
    day: date


@dataclass(frozen=True)
class Standing:
    level: Level
    score: float
    events: int
    probation: bool


@dataclass(frozen=True)
class Plan:
    """How much to check one finished task. ``tokens`` of ``None`` means every traced token."""

    questions: int
    tokens: int | None


def wilson_lower(passes: float, n: float, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval for ``passes`` out of ``n`` (0 for no data)."""
    if n <= 0:
        return 0.0
    p = passes / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - spread) / (1 + z * z / n))


def decayed_score(events: Sequence[Event], today: date, half_life_days: int) -> float:
    passes = total = 0.0
    for event in events:
        weight = 0.5 ** (max((today - event.day).days, 0) / half_life_days)
        total += weight
        passes += weight * event.passed
    return wilson_lower(passes, total)


def standing(events: Sequence[Event], today: date, rules: TeamSettings) -> Standing:
    """Replay the results in order and return where the person stands on ``today``."""
    level = Level.NORMAL
    window: list[bool] = []
    counted: list[Event] = []
    streak = tight_fails = 0
    for event in sorted(events, key=lambda e: (e.day, e.item, e.kind)):
        if event.kind == REVIEW_CLEARED:
            if level is Level.HALTED:
                level, window, streak, tight_fails = Level.NORMAL, [], 0, 0
            continue
        counted.append(event)
        window = [*window, event.passed][-_WINDOW:]
        if level is Level.HALTED:
            continue
        score = decayed_score(counted, event.day, rules.trust_half_life_days)
        if level is Level.NORMAL and len(counted) >= rules.trust_probation_tasks:
            if window.count(False) >= 2:
                level, streak, tight_fails = Level.TIGHTENED, 0, 0
            elif score >= rules.trust_reduced_score:
                level = Level.REDUCED
        elif level is Level.TIGHTENED:
            streak = streak + 1 if event.passed else 0
            tight_fails += not event.passed
            if tight_fails >= rules.trust_halt_fails:
                level = Level.HALTED
            elif streak >= _CLEAN_RUN:
                level = Level.NORMAL
        elif level is Level.REDUCED and (not event.passed or score < rules.trust_reduced_score):
            level = Level.NORMAL
    score = decayed_score(counted, today, rules.trust_half_life_days)
    if level is Level.REDUCED and score < rules.trust_reduced_score:
        level = Level.NORMAL  # a good record that has gone quiet stops earning lighter checks
    return Standing(level, score, len(counted), len(counted) < rules.trust_probation_tasks)


def plan_for(current: Standing, seed: str, rules: TeamSettings) -> Plan | None:
    """The check owed for one task, or ``None`` when none is due now."""
    if current.level is Level.HALTED:
        return None
    if current.level is Level.TIGHTENED:
        return Plan(rules.quiz_max_questions, None)
    if current.level is Level.NORMAL:
        return Plan(min(_NORMAL_QUESTIONS, rules.quiz_max_questions), _NORMAL_TOKENS)
    # Skip-lot: a trusted person is checked on about one task in N, chosen by the seed.
    if sampled(seed, 1 / rules.reduced_skip_lot):
        return Plan(1, _REDUCED_TOKENS)
    return None


class Ledger:
    """Append-only results per subject: a person, or a model feature."""

    TABLES = ("trust_events", "model_events")

    def __init__(self, db: sqlite3.Connection, table: Literal["trust_events", "model_events"]):
        if table not in self.TABLES:
            raise ValueError(f"unknown ledger table {table!r}")
        self.db = db
        self.table = table

    def record(self, event: Event) -> bool:
        """``False`` when this (subject, item, kind) is already on the ledger."""
        cursor = self.db.execute(
            f"INSERT OR IGNORE INTO {self.table} (subject, item, kind, passed, day) "
            "VALUES (?, ?, ?, ?, ?)",
            (event.subject, event.item, event.kind, int(event.passed), event.day.isoformat()),
        )
        return cursor.rowcount == 1

    def events(self, subject: str) -> list[Event]:
        rows = self.db.execute(
            f"SELECT item, kind, passed, day FROM {self.table} WHERE subject = ? "
            "ORDER BY day, item, kind",
            (subject,),
        ).fetchall()
        return [
            Event(subject, item, kind, bool(passed), date.fromisoformat(day))
            for item, kind, passed, day in rows
        ]
