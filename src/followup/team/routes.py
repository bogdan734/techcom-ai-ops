"""Which department a task belongs to: decided when it is first seen, never again.

If a person changes department, their old tasks stay where their sent records are. Moving a task
would put it in a file that has never heard of it, and the first run after the move would send
everything about it again.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date

from followup.models import Task

from .org import Org


def pin_routes(
    control: sqlite3.Connection, tasks: Sequence[Task], org: Org, today: date
) -> dict[str, str]:
    """Record a department for each task not seen before; return every task's department."""
    for task in tasks:
        control.execute(
            "INSERT OR IGNORE INTO task_routes (task_id, department, first_seen) VALUES (?, ?, ?)",
            (task.task_id, org.department_of(task.owner), today.isoformat()),
        )
    return dict(control.execute("SELECT task_id, department FROM task_routes").fetchall())
