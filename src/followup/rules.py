"""Deterministic decision rules.

Dates and statuses alone decide *whether* a task needs attention. The note reading (the
only model output that reaches this module) can change *who* is contacted, and only
between recipients that come from the task data or from configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .models import Blocker, Decision, Status, Task, Verdict


@dataclass(frozen=True)
class Policy:
    escalate_after_days: int = 5
    stale_after_days: int = 5
    due_soon_days: int = 2
    clarify_target: str = "project_manager"
    escalation_target: str = "project_manager"
    materials_target: str = "warehouse"


@dataclass(frozen=True)
class Assessment:
    """Date arithmetic and data completeness for one task on one evaluation date."""

    task: Task
    days_overdue: int
    due_in_days: int | None
    days_since_update: int | None
    missing: tuple[str, ...]

    @property
    def needs_reading(self) -> bool:
        """The note decides the routing only for work that is blocked or late."""
        if self.task.status is Status.DONE or self.missing:
            return False
        return self.task.status is Status.BLOCKED or self.days_overdue > 0


def assess(task: Task, today: date) -> Assessment:
    missing = []
    if task.owner is None:
        missing.append("owner")
    if task.due_date is None:
        missing.append("due date")
    if task.status is Status.UNKNOWN:
        missing.append(
            f"recognised status (got {task.status_raw!r})" if task.status_raw else "status"
        )
    due_in = (task.due_date - today).days if task.due_date else None
    since_update = max(0, (today - task.last_update).days) if task.last_update else None
    return Assessment(
        task=task,
        days_overdue=max(0, -due_in) if due_in is not None else 0,
        due_in_days=due_in,
        days_since_update=since_update,
        missing=tuple(missing),
    )


def decide(assessment: Assessment, blocker: Blocker, policy: Policy) -> Verdict:
    task = assessment.task
    if task.status is Status.DONE:
        return Verdict(Decision.NO_ACTION, "Task is done")
    if assessment.missing:
        return Verdict(
            Decision.CLARIFY,
            "Missing " + _listing(assessment.missing),
            policy.clarify_target,
            "role",
        )

    overdue = assessment.days_overdue
    if task.status is Status.BLOCKED or overdue:
        timing = _timing(assessment)
        if blocker is Blocker.MATERIALS:
            return Verdict(
                Decision.REQUEST_ETA,
                _join("Waiting on materials", timing),
                policy.materials_target,
                "role",
            )
        if blocker is Blocker.DEPENDENCY:
            return Verdict(
                Decision.ESCALATE,
                _join("Waiting on a dependency", timing),
                policy.escalation_target,
                "role",
            )
        if overdue >= policy.escalate_after_days:
            return Verdict(
                Decision.ESCALATE,
                f"Overdue by {_days(overdue)}, escalation threshold is "
                f"{_days(policy.escalate_after_days)}",
                policy.escalation_target,
                "role",
            )
        if task.status is Status.BLOCKED:
            return _to_owner(task, _join("Blocked, blocker not stated", timing))
        return _to_owner(task, f"Overdue by {_days(overdue)}{_NOTE_SUFFIX[blocker]}")

    stale = assessment.days_since_update
    if task.status is Status.IN_PROGRESS and stale is not None and stale >= policy.stale_after_days:
        return _to_owner(task, f"No update for {_days(stale)}")

    due_in = assessment.due_in_days
    if task.status is Status.NOT_STARTED and due_in is not None and due_in <= policy.due_soon_days:
        when = "today" if due_in == 0 else f"in {_days(due_in)}"
        return _to_owner(task, f"Due {when} and not started")

    # Complete data guarantees a due date here, and overdue work returned above.
    return Verdict(
        Decision.NO_ACTION, "On track, due " + ("today" if due_in == 0 else f"in {_days(due_in)}")
    )


_NOTE_SUFFIX = {
    Blocker.NONE: ", no explanation in notes",
    Blocker.UNCLEAR: ", notes do not explain the delay",
    Blocker.UNKNOWN: "",
    Blocker.MATERIALS: "",
    Blocker.DEPENDENCY: "",
}


def _to_owner(task: Task, reason: str) -> Verdict:
    return Verdict(Decision.FOLLOW_UP_OWNER, reason, task.owner, "owner")


def _timing(assessment: Assessment) -> str | None:
    if assessment.days_overdue:
        return f"overdue by {_days(assessment.days_overdue)}"
    if assessment.due_in_days == 0:
        return "due today"
    return None


def _join(head: str, tail: str | None) -> str:
    return f"{head}, {tail}" if tail else head


def _listing(items: tuple[str, ...]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _days(count: int) -> str:
    return f"{count} day" if count == 1 else f"{count} days"
