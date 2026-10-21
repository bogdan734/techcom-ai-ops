from __future__ import annotations

import pytest

from conftest import TODAY, make_task
from followup.models import Blocker, Decision
from followup.rules import Policy, assess, decide

POLICY = Policy()


def verdict(blocker: Blocker = Blocker.NONE, **fields):
    return decide(assess(make_task(**fields), TODAY), blocker, POLICY)


def test_overdue_without_explanation_follows_up_with_owner():
    v = verdict(due_date="2026-10-18", notes="No update since Monday.")

    assert v.decision is Decision.FOLLOW_UP_OWNER
    assert (v.target, v.target_kind) == ("John Smith", "owner")
    assert v.reason == "Overdue by 3 days, no explanation in notes"


def test_overdue_and_blocked_by_materials_requests_eta_from_warehouse():
    v = verdict(Blocker.MATERIALS, status="Blocked", due_date="2026-10-18")

    assert v.decision is Decision.REQUEST_ETA
    assert (v.target, v.target_kind) == ("warehouse", "role")
    assert v.reason == "Waiting on materials, overdue by 3 days"


def test_materials_note_on_late_task_routes_to_warehouse_even_if_status_is_not_blocked():
    v = verdict(Blocker.MATERIALS, due_date="2026-10-19")

    assert v.decision is Decision.REQUEST_ETA


def test_blocked_by_dependency_escalates_to_project_manager():
    v = verdict(Blocker.DEPENDENCY, due_date="2026-10-15", last_update="2026-10-15")

    assert v.decision is Decision.ESCALATE
    assert v.target == "project_manager"
    assert v.reason == "Waiting on a dependency, overdue by 6 days"


@pytest.mark.parametrize("status", ["Done", "Completed"])
def test_done_task_needs_nothing_even_when_overdue_or_incomplete(status):
    v = verdict(status=status, due_date="2026-01-01", assigned_to=None)

    assert v.decision is Decision.NO_ACTION
    assert v.target is None


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"assigned_to": None}, "Missing owner"),
        ({"due_date": None}, "Missing due date"),
        ({"due_date": "10/18/2026"}, "Missing due date"),
        ({"assigned_to": "", "due_date": None}, "Missing owner and due date"),
        ({"status": "On hold"}, "Missing recognised status (got 'On hold')"),
    ],
)
def test_missing_data_asks_for_clarification_instead_of_guessing(fields, reason):
    v = verdict(Blocker.MATERIALS, **fields)

    assert v.decision is Decision.CLARIFY
    assert (v.target, v.target_kind) == ("project_manager", "role")
    assert v.reason == reason


def test_long_overdue_task_is_escalated():
    v = verdict(due_date="2026-10-14")

    assert v.decision is Decision.ESCALATE
    assert v.reason == "Overdue by 7 days, escalation threshold is 5 days"


def test_blocked_without_stated_blocker_asks_owner():
    v = verdict(Blocker.UNCLEAR, status="Blocked", due_date="2026-10-21")

    assert v.decision is Decision.FOLLOW_UP_OWNER
    assert v.reason == "Blocked, blocker not stated, due today"


def test_unread_note_does_not_change_the_date_based_decision():
    v = verdict(Blocker.UNKNOWN, due_date="2026-10-20")

    assert v.decision is Decision.FOLLOW_UP_OWNER
    assert v.reason == "Overdue by 1 day"


def test_stale_work_in_progress_gets_a_status_check():
    v = verdict(last_update="2026-10-14")

    assert v.decision is Decision.FOLLOW_UP_OWNER
    assert v.reason == "No update for 7 days"


def test_not_started_and_due_soon_gets_a_reminder():
    v = verdict(status="Not Started", due_date="2026-10-23")

    assert v.decision is Decision.FOLLOW_UP_OWNER
    assert v.reason == "Due in 2 days and not started"


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "Not Started", "due_date": "2026-10-27", "notes": "Scheduled next week."},
        {"due_date": "2026-10-21"},
        {"notes": "Waiting on materials from the warehouse."},
    ],
)
def test_on_track_tasks_need_no_action_whatever_the_note_says(fields):
    v = verdict(Blocker.MATERIALS, **fields)

    assert v.decision is Decision.NO_ACTION


def test_note_is_read_only_for_blocked_or_late_work():
    def needs(**fields):
        return assess(make_task(**fields), TODAY).needs_reading

    assert needs(status="Blocked")
    assert needs(due_date="2026-10-20")
    assert not needs()
    assert not needs(status="Done", due_date="2026-10-01")
    assert not needs(due_date="2026-10-01", assigned_to=None)


def test_thresholds_and_targets_come_from_policy():
    policy = Policy(escalate_after_days=2, escalation_target="ops_lead")
    v = decide(assess(make_task(due_date="2026-10-18"), TODAY), Blocker.NONE, policy)

    assert (v.decision, v.target) == (Decision.ESCALATE, "ops_lead")


@pytest.mark.parametrize(
    ("due_date", "decision"),
    [("2026-10-16", Decision.ESCALATE), ("2026-10-17", Decision.FOLLOW_UP_OWNER)],
)
def test_escalation_starts_exactly_at_the_threshold(due_date, decision):
    assert verdict(due_date=due_date).decision is decision  # 5 days overdue vs 4


@pytest.mark.parametrize(
    ("last_update", "decision"),
    [("2026-10-16", Decision.FOLLOW_UP_OWNER), ("2026-10-17", Decision.NO_ACTION)],
)
def test_status_check_starts_exactly_at_the_staleness_threshold(last_update, decision):
    assert verdict(last_update=last_update).decision is decision  # 5 days without update vs 4
