"""Domain types shared by every stage of a run."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class Status(StrEnum):
    NOT_STARTED = "Not Started"
    IN_PROGRESS = "Working on it"
    BLOCKED = "Blocked"
    DONE = "Done"
    UNKNOWN = "Unknown"


class Decision(StrEnum):
    NO_ACTION = "no_action"
    FOLLOW_UP_OWNER = "follow_up_owner"
    REQUEST_ETA = "request_eta"
    CLARIFY = "clarify_missing_info"
    ESCALATE = "escalate"


class Blocker(StrEnum):
    """What a task note says is holding the work up."""

    NONE = "none"
    MATERIALS = "materials"
    DEPENDENCY = "dependency"
    UNCLEAR = "unclear"
    # Not read at all: empty budget, model unavailable, invalid output or a flagged note.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Task:
    """A task after normalisation. ``issues`` lists what was wrong with the raw record."""

    task_id: str
    project: str
    title: str
    owner: str | None
    status: Status
    status_raw: str
    due_date: date | None
    last_update: date | None
    notes: str
    issues: tuple[str, ...] = ()

    def fingerprint(self) -> str:
        """Hash of the fields a decision depends on; any change means a new task state."""
        state = {
            "status": self.status_raw if self.status is Status.UNKNOWN else self.status.value,
            "owner": self.owner,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "last_update": self.last_update.isoformat() if self.last_update else None,
            "notes": self.notes,
        }
        return _digest(state)


@dataclass(frozen=True)
class Verdict:
    """What the rules decided for one task."""

    decision: Decision
    reason: str
    target: str | None = None
    # "owner" when the message goes to the task owner, "role" for a team mailbox.
    target_kind: str | None = None


@dataclass(frozen=True)
class Reading:
    """How a note was interpreted and where the interpretation came from."""

    blocker: Blocker
    source: str
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Action:
    """An outbound message, identified by what it is about rather than by its wording."""

    action_id: str
    task_id: str
    decision: Decision
    target: str
    reason: str
    message: str
    drafted_by: str


def action_id(task: Task, verdict: Verdict) -> str:
    """Stable operation identity: same task state, decision and recipient -> same id.

    The message text is deliberately not part of it: drafts differ between runs, and a
    re-worded draft of the same follow-up is still the same follow-up.
    """
    return _digest(
        {
            "task_id": task.task_id,
            "decision": verdict.decision.value,
            "target": verdict.target,
            "state": task.fingerprint(),
        }
    )


@dataclass
class TaskOutcome:
    """One line of ``decisions.json``."""

    task_id: str
    project: str
    task: str
    decision: str
    target: str | None
    reason: str
    message: str | None = None
    action_id: str | None = None
    delivery: str = "none"
    message_id: str | None = None
    drafted_by: str | None = None
    note_reading: dict[str, str] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


def _digest(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
