"""Raw API records -> :class:`Task`.

Nothing here guesses. A value that cannot be read with certainty becomes ``None`` and an
entry in ``Task.issues``, and the rules turn missing data into a clarification request.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from .models import Status, Task

MAX_NOTE_LENGTH = 1000

_STATUS_ALIASES = {
    "not started": Status.NOT_STARTED,
    "to do": Status.NOT_STARTED,
    "todo": Status.NOT_STARTED,
    "working on it": Status.IN_PROGRESS,
    "in progress": Status.IN_PROGRESS,
    "blocked": Status.BLOCKED,
    "done": Status.DONE,
    "complete": Status.DONE,
    "completed": Status.DONE,
}
_NO_OWNER = {"", "null", "none", "n/a", "na", "unassigned", "tbd", "-", "?"}
_TASK_ID = re.compile(r"^[A-Z0-9][A-Z0-9_.:-]{0,63}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class InvalidRecord(ValueError):
    """The record has no usable identity, so it cannot be acted on at all."""


def normalise_task(raw: Mapping[str, Any]) -> Task:
    issues: list[str] = []
    task_id = _task_id(raw.get("task_id"))
    status, status_raw = _status(raw.get("status"), issues)
    return Task(
        task_id=task_id,
        project=_text(raw.get("project")),
        title=_text(raw.get("task")),
        owner=_owner(raw.get("assigned_to"), issues),
        status=status,
        status_raw=status_raw,
        due_date=_date(raw.get("due_date"), "due_date", issues),
        last_update=_date(raw.get("last_update"), "last_update", issues),
        notes=_notes(raw.get("notes"), issues),
        issues=tuple(issues),
    )


def normalise_all(records: Iterable[Mapping[str, Any]]) -> tuple[list[Task], list[str]]:
    """Normalise a batch; returns the tasks and a description of every rejected record.

    A task id seen twice keeps the record with the most recent ``last_update``: the API
    may return a task twice while it is being edited, and the newer copy is the truth.
    """
    tasks: dict[str, Task] = {}
    rejected: list[str] = []
    for position, raw in enumerate(records):
        if not isinstance(raw, Mapping):
            rejected.append(f"record #{position}: expected an object, got {type(raw).__name__}")
            continue
        try:
            task = normalise_task(raw)
        except InvalidRecord as exc:
            rejected.append(f"record #{position}: {exc}")
            continue
        previous = tasks.get(task.task_id)
        if previous is None:
            tasks[task.task_id] = task
            continue
        kept = task if _newer(task, previous) else previous
        tasks[task.task_id] = kept
        rejected.append(
            f"record #{position}: duplicate {task.task_id}, "
            f"kept the copy last updated {kept.last_update or 'never'}"
        )
    return list(tasks.values()), rejected


def _newer(candidate: Task, current: Task) -> bool:
    if candidate.last_update is None:
        return False
    return current.last_update is None or candidate.last_update > current.last_update


def _task_id(value: Any) -> str:
    if not isinstance(value, str | int) or isinstance(value, bool):
        raise InvalidRecord(f"task_id missing or not a string: {value!r}")
    task_id = str(value).strip().upper()
    if not _TASK_ID.match(task_id):
        raise InvalidRecord(f"task_id {value!r} is not a valid identifier")
    return task_id


def _status(value: Any, issues: list[str]) -> tuple[Status, str]:
    raw = _text(value)
    key = re.sub(r"[\s_]+", " ", raw.lower()).strip()
    status = _STATUS_ALIASES.get(key)
    if status is None:
        issues.append(f"status {raw!r} is not recognised" if raw else "status is missing")
        return Status.UNKNOWN, raw
    return status, raw


def _owner(value: Any, issues: list[str]) -> str | None:
    if value is not None and not isinstance(value, str):
        issues.append(f"assigned_to {value!r} is not a name")
        return None
    owner = _text(value)
    return None if owner.lower() in _NO_OWNER else owner


def _date(value: Any, name: str, issues: list[str]) -> date | None:
    """ISO dates only: ``10/11/2026`` could be either day-first or month-first."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        text = value.strip()
        try:
            if "T" in text or " " in text:
                stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if stamp.tzinfo is not None:
                    stamp = stamp.astimezone(UTC)
                return stamp.date()
            return date.fromisoformat(text.replace("/", "-"))
        except ValueError:
            pass
    issues.append(f"{name} {value!r} is not an ISO date")
    return None


def _notes(value: Any, issues: list[str]) -> str:
    notes = _text(value if isinstance(value, str) or value is None else str(value))
    notes = re.sub(r"\s+", " ", _CONTROL.sub(" ", notes)).strip()
    if len(notes) > MAX_NOTE_LENGTH:
        issues.append(f"notes truncated to {MAX_NOTE_LENGTH} characters")
        notes = notes[:MAX_NOTE_LENGTH]
    return notes


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""
