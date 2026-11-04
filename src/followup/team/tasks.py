"""Tasks made in the app, and the moves people make on tasks, over a task source we do not own.

The tasks API is read-only here. A task an issuer creates lives in the control file and is
added to what the API returns. Accepting, handing in and returning a task are events; the last
one decides the status the run sees. Writing the same events back to the tracker is the one
integration a real deployment adds, and the events are what it would send.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from .schema import CONTROL
from .vault import Vault

STATUS_FOR = {"accepted": "Working on it", "submitted": "Done", "returned": "Working on it"}


@dataclass(frozen=True)
class TaskEvent:
    seq: int
    task_id: str
    kind: str
    person_id: str
    day: date
    note: str


def normal_id(value: object) -> str:
    return " ".join(str(value).split()).upper()


def events(vault: Vault) -> list[TaskEvent]:
    rows = _read(
        vault, "SELECT seq, task_id, kind, person_id, day, note FROM task_events ORDER BY seq"
    )
    return [TaskEvent(s, normal_id(t), k, p, date.fromisoformat(d), n) for s, t, k, p, d, n in rows]


def authored_records(vault: Vault) -> list[dict[str, Any]]:
    rows = _read(vault, "SELECT record FROM authored_tasks ORDER BY task_id")
    return [json.loads(record) for (record,) in rows]


def last_event(all_events: Sequence[TaskEvent], task_id: str) -> TaskEvent | None:
    mine = [e for e in all_events if e.task_id == normal_id(task_id)]
    return mine[-1] if mine else None


def effective_records(vault: Vault, records: Sequence[Any]) -> list[Any]:
    """What the tasks API said, plus the tasks made here, with the latest move on each applied."""
    merged = [*records, *authored_records(vault)]
    latest = {e.task_id: e for e in events(vault)}
    result: list[Any] = []
    for record in merged:
        task_id = record.get("task_id") if isinstance(record, dict) else None
        event = latest.get(normal_id(task_id)) if task_id is not None else None
        if event is None:
            result.append(record)
        else:
            result.append(
                {**record, "status": STATUS_FOR[event.kind], "last_update": event.day.isoformat()}
            )
    return result


def next_task_id(db: sqlite3.Connection) -> str:
    rows = db.execute("SELECT task_id FROM authored_tasks").fetchall()
    highest = max((int(task_id.rsplit("-", 1)[-1]) for (task_id,) in rows), default=0)
    return f"APP-{highest + 1:04d}"


def _read(vault: Vault, query: str) -> list[tuple]:
    if not vault.path(CONTROL).exists():
        return []
    db = vault.open(CONTROL)
    try:
        return db.execute(query).fetchall()
    finally:
        db.close()
