"""Phone calls as a delivery channel: the same outbox guarantees, plus consent and a window.

A call is a message with another route. It carries the same idempotency key, a lost response is
reconciled with a lookup before anything is dialled again, and a person gets a call only after
a message and a reminder went unanswered. The provider reports who picked up; a machine counts
for a reminder and does not for a check that needs a person. There is no speech-to-text, so
the answer to a quiz still comes through chat or the app.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, Literal

import httpx2

from followup.api import MessagesApi
from followup.retry import RetryPolicy

from .notices import Channel, Kind
from .org import Person
from .settings import TeamSettings

Purpose = Literal["reminder", "verification"]


class AnsweredBy(StrEnum):
    PERSON = "person"
    MACHINE = "machine"
    NONE = "none"
    # The provider has not said yet; a poll or a webhook settles it.
    PENDING = "pending"


class CallsApi(MessagesApi):
    """The voice provider: ``/calls`` where messages use ``/messages``, nothing else differs.

    ``extras`` adds fields to the body of one call (what the call is for, the questions to read,
    where to send the answers); the idempotency key and the reconcile-before-resend rule are the
    message path's, unchanged.
    """

    PATH = "/calls"

    def __init__(
        self,
        http: httpx2.Client,
        policy: RetryPolicy,
        extras: dict[str, Mapping[str, Any]] | None = None,
    ) -> None:
        super().__init__(http, policy)
        self.extras: dict[str, Mapping[str, Any]] = extras if extras is not None else {}

    def post(self, idempotency_key: str, payload: dict[str, Any]) -> httpx2.Response:
        return super().post(idempotency_key, {**payload, **self.extras.get(idempotency_key, {})})


def provider_client(team: TeamSettings) -> httpx2.Client | None:
    """A client for a real voice provider, or ``None`` to use the mock beside the messages."""
    if not team.calls or team.calls_provider != "http":
        return None
    headers = {"Authorization": f"Bearer {team.calls_api_key}"} if team.calls_api_key else {}
    return httpx2.Client(base_url=team.calls_api_url, headers=headers, timeout=30.0)


@dataclass(frozen=True)
class Rung:
    step: int
    kind: Kind
    channel: Channel
    purpose: Purpose


def ladder(
    issued_on: date,
    today: date,
    step_days: int,
    callable_now: bool,
    purpose: Purpose,
    call_purpose: Purpose = "reminder",
) -> list[Rung]:
    """The rungs due so far: the check at once, a reminder after one step, a call after two."""
    waited = max((today - issued_on).days, 0) // step_days
    rungs = [Rung(0, Kind.QUIZ, Channel.MESSAGE, purpose)]
    if waited >= 1:
        rungs.append(Rung(1, Kind.QUIZ_REMINDER, Channel.MESSAGE, "reminder"))
    if waited >= 2 and callable_now:
        rungs.append(Rung(2, Kind.QUIZ_CALL, Channel.CALL, call_purpose))
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
    call_key: str,
    person_id: str,
    task_id: str,
    answered: AnsweredBy,
    purpose: Purpose,
    day: date,
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO call_results "
        "(call_key, person_id, task_id, answered_by, purpose, placed_on) VALUES (?, ?, ?, ?, ?, ?)",
        (call_key, person_id, task_id, answered.value, purpose, day.isoformat()),
    )


def settle_result(db: sqlite3.Connection, call_key: str, answered: AnsweredBy) -> bool:
    """Replace ``pending`` with what the provider reported; a settled result never changes."""
    if answered is AnsweredBy.PENDING:
        return False
    cursor = db.execute(
        "UPDATE call_results SET answered_by = ? WHERE call_key = ? AND answered_by = 'pending'",
        (answered.value, call_key),
    )
    return cursor.rowcount == 1


def pending_calls(db: sqlite3.Connection) -> list[str]:
    rows = db.execute("SELECT call_key FROM call_results WHERE answered_by = 'pending' ORDER BY 1")
    return [key for (key,) in rows.fetchall()]


def results(db: sqlite3.Connection) -> dict[str, tuple[AnsweredBy, Purpose]]:
    rows = db.execute("SELECT call_key, answered_by, purpose FROM call_results ORDER BY call_key")
    return {key: (AnsweredBy(who), purpose) for key, who, purpose in rows.fetchall()}
