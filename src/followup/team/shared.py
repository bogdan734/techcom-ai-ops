"""A task given to several people is one part per person, and this reads the parts back as one.

Every part is an ordinary task with its own id, owner, moves, check and thread, so the rules,
the check levels and the data separation work on it unchanged. What binds the parts is the group
id each one carries, and one rule: the shared task is done only when every part is.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from followup.models import Status, Task

from .extras import TaskExtras


@dataclass(frozen=True)
class Shared:
    group: str
    parts: tuple[Task, ...]  # by task id

    @property
    def done(self) -> int:
        return sum(part.status is Status.DONE for part in self.parts)

    @property
    def status(self) -> Status:
        if self.done == len(self.parts):
            return Status.DONE
        if all(part.status is Status.NOT_STARTED for part in self.parts):
            return Status.NOT_STARTED
        return Status.IN_PROGRESS


def shared_tasks(tasks: Sequence[Task], extras: Mapping[str, TaskExtras]) -> dict[str, Shared]:
    """The tasks given to several people, by group id; a task for one person is not in it."""
    parts: dict[str, list[Task]] = {}
    for task in sorted(tasks, key=lambda t: t.task_id):
        extra = extras.get(task.task_id)
        if extra is not None and extra.group is not None:
            parts.setdefault(extra.group, []).append(task)
    return {group: Shared(group, tuple(found)) for group, found in parts.items() if len(found) > 1}
