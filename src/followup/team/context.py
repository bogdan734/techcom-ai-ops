"""Everything the console and the app need to read one team's state, built from the settings.

It reads the tasks file the way a run does and opens nothing for writing; the vault opens each
department file when asked. The CLI and the web app share it, so the two cannot disagree about
who a person is, where a task lives or what a viewer may see.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from typing import Any

from followup.config import ConfigError, Settings
from followup.models import Task
from followup.normalize import normalise_all

from .extras import TaskExtras, load_extras
from .org import Org, Person, Role
from .schema import CONTROL
from .settings import TeamSettings
from .tasks import effective_records
from .vault import Vault


class ActionError(Exception):
    """A request was refused: unknown person, not allowed, or not possible now."""


@dataclass
class Context:
    settings: Settings
    team: TeamSettings
    vault: Vault
    records: list[Any]
    tasks: list[Task]
    extras: dict[str, TaskExtras]

    @property
    def org(self) -> Org:
        return self.team.org

    @property
    def today(self) -> date:
        return self.settings.today

    def person(self, pid: str) -> Person:
        """The person acting. Mailbox roles receive messages and cannot act."""
        found = self.org.find(pid)
        if found is None:
            raise ActionError(f"unknown person {pid!r}")
        if found.role in (Role.PROJECT_MANAGER, Role.WAREHOUSE):
            raise ActionError(f"{found.id} is a mailbox, not a person who can act")
        return found

    def task(self, task_id: str) -> Task:
        wanted = " ".join(task_id.split()).upper()
        found = next((t for t in self.tasks if t.task_id == wanted), None)
        if found is None:
            raise ActionError(f"unknown task {task_id!r}")
        return found

    def department(self, task: Task) -> str:
        """Where the task lives: the department pinned when it was first seen."""
        pinned = self.routes().get(task.task_id)
        return pinned or self.org.department_of(task.owner)

    def routes(self) -> dict[str, str]:
        path = self.vault.path(CONTROL)
        if not path.exists():
            return {}
        db = self.vault.open(CONTROL)
        try:
            return dict(db.execute("SELECT task_id, department FROM task_routes").fetchall())
        finally:
            db.close()

    def open(self, name: str) -> sqlite3.Connection | None:
        """A service connection to a department file, or ``None`` before the first run."""
        return self.vault.open(name) if self.vault.path(name).exists() else None


def load_context(settings: Settings) -> Context:
    team = settings.team
    if team is None:
        raise ConfigError("no org file: set ORG_FILE or pass --org")
    try:
        records = json.loads(settings.tasks_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read tasks file {settings.tasks_file}: {exc}") from exc
    if not isinstance(records, list):
        raise ConfigError(f"tasks file {settings.tasks_file} must contain a JSON array")
    vault = Vault(settings.out_dir / "state", team.org, team.trust_visibility)
    records = effective_records(vault, records)
    tasks, _ = normalise_all(records)
    return Context(settings, team, vault, records, tasks, load_extras(records, team.org))


def read_all(db: sqlite3.Connection | None, query: str, args: tuple = ()) -> list[tuple]:
    if db is None:
        return []
    with closing(db.cursor()) as cursor:
        return cursor.execute(query, args).fetchall()
