"""Phone calls as a delivery channel: the same outbox guarantees, plus consent and a window.

A call is a message with another route. It carries the same idempotency key, a lost response is
reconciled with a lookup before anything is dialled again, and a person gets a call only after
a message and a reminder went unanswered. The provider reports who picked up; a machine counts
for a reminder and does not for a check that needs a person. There is no speech-to-text, so
the answer to a quiz still comes through chat or the app.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Literal

from followup.api import MessagesApi

from .notices import Channel, Kind
from .org import Person
from .settings import TeamSettings

Purpose = Literal["reminder", "verification"]


class AnsweredBy(StrEnum):
    PERSON = "person"
    MACHINE = "machine"
    NONE = "none"


class CallsApi(MessagesApi):
    """The voice provider: ``/calls`` where messages use ``/messages``, nothing else differs."""

    PATH = "/calls"


@dataclass(frozen=True)
class Rung:
    step: int
    kind: Kind
    channel: Channel
    purpose: Purpose


def ladder(
    issued_on: date, today: date, step_days: int, callable_now: bool, purpose: Purpose
) -> list[Rung]:
    """The rungs due so far: the check at once, a reminder after one step, a call after two."""
    waited = max((today - issued_on).days, 0) // step_days
    rungs = [Rung(0, Kind.QUIZ, Channel.MESSAGE, purpose)]
    if waited >= 1:
        rungs.append(Rung(1, Kind.QUIZ_REMINDER, Channel.MESSAGE, "reminder"))
    if waited >= 2 and callable_now:
        rungs.append(Rung(2, Kind.QUIZ_CALL, Channel.CALL, "reminder"))
    return rungs


def can_call(person: Person, team: TeamSettings) -> bool:
    """Calls are on, the person has a number and agreed, and it is inside the calling window."""
    return bool(team.calls and person.phone and person.call_consent and team.call_open())


def counts_as_delivered(purpose: Purpose, answered: AnsweredBy) -> bool:
    if answered is AnsweredBy.PERSON:
        return True
    return purpose == "reminder" and answered is AnsweredBy.MACHINE


def record_result(
    db: sqlite3.Connection,
    action_id: str,
    person_id: str,
    answered: AnsweredBy,
    purpose: Purpose,
    day: date,
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO call_results (call_key, person_id, answered_by, purpose, placed_on) "
        "VALUES (?, ?, ?, ?, ?)",
        (action_id, person_id, answered.value, purpose, day.isoformat()),
    )


def results(db: sqlite3.Connection) -> dict[str, tuple[AnsweredBy, Purpose]]:
    rows = db.execute("SELECT call_key, answered_by, purpose FROM call_results ORDER BY call_key")
    return {key: (AnsweredBy(who), purpose) for key, who, purpose in rows.fetchall()}
