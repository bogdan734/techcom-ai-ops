"""Reminder cadence: round identities, the interval between rounds, the final escalation."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta

import pytest

from conftest import FIXTURE, TODAY, RecordingStub, make_task, no_wait_policy, raw_task
from followup.config import Settings
from followup.models import Decision, Verdict, action_id
from followup.pipeline import RunReport, run

# TC-1002 as docs/sample/decisions.json has it: written before reminders existed.
SUBMITTED_TC_1002_ID = "6feae298eded94f420882f212db7e84f"
CADENCE = {"REMIND_EVERY_DAYS": "2", "MAX_REMINDERS": "2"}
# Seven days without an update on the first day, so its owner is followed up; due only on 10-30.
STALE_TASK = raw_task(last_update="2026-10-14")
OWNER = ("follow_up_owner", "John Smith")
ESCALATION = ("escalate", "project_manager")


def settings_for(tmp_path, records: list[dict], **env: str) -> Settings:
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps(records))
    base = {"EVALUATION_DATE": TODAY.isoformat(), "TASKS_FILE": str(tasks), **env}
    return replace(Settings.from_env(base), out_dir=tmp_path / "out")


@pytest.fixture
def cadence(tmp_path):
    return settings_for(tmp_path, [STALE_TASK], **CADENCE)


def go(settings, day: date, faults=None) -> RunReport:
    return run(replace(settings, today=day), faults, RecordingStub(), no_wait_policy())


def run_days(settings, first: date, last: date) -> dict[date, RunReport]:
    """One run per evaluation day over the same out directory, as a daily schedule would."""
    days = (first + timedelta(days=n) for n in range((last - first).days + 1))
    return {day: go(settings, day) for day in days}


def sent_each_day(reports: dict[date, RunReport]) -> dict[date, list[tuple[str, str | None]]]:
    """(decision, recipient) of every message that went out, on the days one did."""
    sent = {
        day: [(o.decision, o.target) for o in report.outcomes if o.delivery == "sent"]
        for day, report in reports.items()
    }
    return {day: messages for day, messages in sent.items() if messages}


def only(report: RunReport):
    (outcome,) = report.outcomes
    return outcome


def delivered(settings) -> list[dict]:
    path = settings.out_dir / "delivery_log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_round_zero_keeps_the_submitted_identity(tmp_path):
    settings = settings_for(tmp_path, json.loads(FIXTURE.read_text()), **CADENCE)

    report = go(settings, TODAY)

    tc_1002 = next(o for o in report.outcomes if o.task_id == "TC-1002")
    assert tc_1002.action_id == SUBMITTED_TC_1002_ID


def test_repeat_rounds_get_their_own_identities():
    task = make_task(last_update="2026-10-14")
    verdict = Verdict(Decision.FOLLOW_UP_OWNER, "No update for 7 days", "John Smith", "owner")

    ids = [action_id(task, verdict, repeat) for repeat in (0, 1, 2)]

    assert len(set(ids)) == 3
    assert ids[0] == action_id(task, verdict)


def test_unchanged_task_is_reminded_every_n_days_then_escalated_once(cadence):
    reports = run_days(cadence, TODAY, date(2026, 11, 4))

    assert sent_each_day(reports) == {
        date(2026, 10, 21): [OWNER],
        date(2026, 10, 23): [OWNER],
        date(2026, 10, 25): [OWNER],
        date(2026, 10, 27): [ESCALATION],
    }
    assert len(delivered(cadence)) == 4
    assert [only(reports[day]).reason for day in sorted(sent_each_day(reports))] == [
        "No update for 7 days",
        "No update for 9 days, reminder 1 of 2",
        "No update for 11 days, reminder 2 of 2",
        "No update for 13 days, unchanged after 2 reminder(s)",
    ]
    # On 11-04 the task is 5 days overdue, which is the threshold escalation. It has the identity
    # of the final escalation that went out on 10-27, so it is not sent a second time.
    last = only(reports[date(2026, 11, 4)])
    assert (last.decision, last.delivery) == ("escalate", "duplicate_suppressed")


def test_cadence_off_sends_one_follow_up_per_state(tmp_path):
    off = settings_for(tmp_path, [STALE_TASK])

    reports = run_days(off, TODAY, date(2026, 11, 4))

    # The second message is the threshold escalation on 11-04, a new state of the rules.
    assert sent_each_day(reports) == {
        date(2026, 10, 21): [OWNER],
        date(2026, 11, 4): [ESCALATION],
    }


def test_an_undelivered_round_holds_the_cadence(cadence):
    failed = go(cadence, TODAY, faults={"send": ["503"] * 4})

    reports = run_days(cadence, date(2026, 10, 23), date(2026, 10, 25))

    assert only(failed).delivery == "failed"
    # The retry goes out under the key of 10-21; the interval counts from this delivery, so
    # reminder 1 is not due on 10-23 or 10-24 but on 10-25.
    assert sent_each_day(reports) == {date(2026, 10, 23): [OWNER], date(2026, 10, 25): [OWNER]}
    assert only(reports[date(2026, 10, 23)]).action_id == only(failed).action_id
    assert only(reports[date(2026, 10, 25)]).action_id != only(failed).action_id
    assert len(delivered(cadence)) == 2


def test_a_changed_task_starts_again_at_round_zero(cadence):
    run_days(cadence, TODAY, date(2026, 10, 23))
    changed = raw_task(last_update="2026-10-19")
    cadence.tasks_file.write_text(json.dumps([changed]))

    report = go(cadence, date(2026, 10, 24))

    # Reminder 1 went out the day before, but a new state does not wait for an interval.
    verdict = Verdict(Decision.FOLLOW_UP_OWNER, "No update for 5 days", "John Smith", "owner")
    outcome = only(report)
    assert (outcome.delivery, outcome.reason) == ("sent", "No update for 5 days")
    assert outcome.action_id == action_id(make_task(last_update="2026-10-19"), verdict)
    assert len(delivered(cadence)) == 3


def test_escalations_are_not_repeated(tmp_path):
    records = [r for r in json.loads(FIXTURE.read_text()) if r["task_id"] == "TC-1008"]
    escalating = settings_for(tmp_path, records, **CADENCE)

    reports = run_days(escalating, TODAY, date(2026, 10, 27))

    assert [m["task_id"] for m in delivered(escalating)] == ["TC-1008"]
    later = {only(report).delivery for day, report in reports.items() if day > TODAY}
    assert later == {"duplicate_suppressed"}


def test_final_escalation_waits_for_approval_when_the_gate_is_on(tmp_path):
    gated = settings_for(tmp_path, [STALE_TASK], APPROVE_ESCALATIONS="true", **CADENCE)

    reports = run_days(gated, TODAY, date(2026, 10, 27))

    assert only(reports[date(2026, 10, 27)]).delivery == "awaiting_approval"
    # Only the escalation is gated: the three messages to the owner went out.
    assert sent_each_day(reports) == {
        date(2026, 10, 21): [OWNER],
        date(2026, 10, 23): [OWNER],
        date(2026, 10, 25): [OWNER],
    }
    assert len(delivered(gated)) == 3
