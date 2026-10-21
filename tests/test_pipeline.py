"""End-to-end runs over the supplied fixture: the scenarios the assessment requires."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

from conftest import FIXTURE, TODAY, RecordingStub, ScriptedLLM, no_wait_policy
from followup.config import Settings
from followup.pipeline import run

EXPECTED = {
    "TC-1001": ("follow_up_owner", "John Smith"),
    "TC-1002": ("request_eta", "warehouse"),
    "TC-1003": ("no_action", None),
    "TC-1004": ("clarify_missing_info", "project_manager"),
    "TC-1005": ("clarify_missing_info", "project_manager"),
    "TC-1006": ("follow_up_owner", "Jamie Lee"),
    "TC-1007": ("no_action", None),
    "TC-1008": ("escalate", "project_manager"),
    "TC-1009": ("request_eta", "warehouse"),
    "TC-1010": ("no_action", None),
}


@pytest.fixture
def settings(tmp_path):
    env = {"EVALUATION_DATE": TODAY.isoformat(), "TASKS_FILE": str(FIXTURE)}
    return replace(Settings.from_env(env), out_dir=tmp_path / "out")


def go(settings, faults=None, llm=None):
    return run(settings, faults, llm or RecordingStub(), no_wait_policy())


def delivered(settings) -> list[dict]:
    path = settings.out_dir / "delivery_log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_fixture_decisions_and_deliveries(settings):
    report = go(settings)

    assert {o.task_id: (o.decision, o.target) for o in report.outcomes} == EXPECTED
    assert report.exit_code == 0
    assert sorted(m["task_id"] for m in delivered(settings)) == sorted(
        task_id for task_id, (decision, _) in EXPECTED.items() if decision != "no_action"
    )
    written = json.loads((settings.out_dir / "decisions.json").read_text())
    assert [d["task_id"] for d in written] == list(EXPECTED)
    assert {"task_id", "decision", "target", "reason", "message"} <= set(written[0])


def test_second_run_with_unchanged_data_sends_nothing_and_asks_the_model_nothing(settings):
    go(settings)
    llm = RecordingStub()

    report = go(settings, llm=llm)

    assert len(delivered(settings)) == 7
    actionable = [o for o in report.outcomes if o.decision != "no_action"]
    assert {o.delivery for o in actionable} == {"duplicate_suppressed"}
    assert all(o.message for o in actionable)
    assert llm.requests == []


def test_changed_task_gets_a_new_follow_up_and_nothing_else_does(settings, tmp_path):
    go(settings)
    records = json.loads(FIXTURE.read_text())
    records[0]["last_update"] = "2026-10-21"
    records[0]["notes"] = "Still splicing, crew short-handed."
    changed = tmp_path / "tasks.json"
    changed.write_text(json.dumps(records))

    report = go(replace(settings, tasks_file=changed))

    sent = [o.task_id for o in report.outcomes if o.delivery == "sent"]
    assert sent == ["TC-1001"]
    assert len(delivered(settings)) == 8


def test_follow_up_turns_into_one_escalation_when_the_threshold_is_crossed(settings):
    go(settings)
    three_days_later = replace(settings, today=date(2026, 10, 24))

    later = go(three_days_later)
    again = go(three_days_later)

    # TC-1001 is now 6 days overdue: its owner was already reminded, so it escalates;
    # every other task keeps its decision and its state, so nothing else goes out.
    assert {o.task_id: o.decision for o in later.outcomes if o.delivery == "sent"} == {
        "TC-1001": "escalate"
    }
    assert [o for o in again.outcomes if o.delivery == "sent"] == []


def test_rate_limited_tasks_api_is_retried(settings):
    report = go(settings, faults={"tasks": ["429", "503", "502"]})

    assert report.exit_code == 0
    assert len(report.outcomes) == 10


def test_unavailable_tasks_api_aborts_without_side_effects(settings):
    report = go(settings, faults={"tasks": ["503"] * 4})

    assert report.exit_code == 1
    assert "gave up after 4 attempt(s)" in report.aborted
    assert report.outcomes == []
    assert delivered(settings) == []


def test_timeout_after_commit_does_not_duplicate_the_message(settings):
    report = go(settings, faults={"send": ["timeout-after-commit"]})

    first = next(o for o in report.outcomes if o.delivery != "none")
    assert (first.task_id, first.delivery) == ("TC-1001", "reconciled")
    assert len(delivered(settings)) == 7
    assert len({m["idempotency_key"] for m in delivered(settings)}) == 7


def test_prompt_injection_is_treated_as_data(settings):
    llm = RecordingStub()

    report = go(settings, llm=llm)

    tc1006 = next(o for o in report.outcomes if o.task_id == "TC-1006")
    assert (tc1006.decision, tc1006.target) == ("follow_up_owner", "Jamie Lee")
    assert tc1006.flags == ["prompt_injection_suspected"]
    assert "Ignore previous instructions" not in json.dumps([r.prompt for r in llm.requests])
    assert {o.task_id for o in report.outcomes if o.decision == "no_action"} == {
        "TC-1003",
        "TC-1007",
        "TC-1010",
    }


def test_garbage_from_the_model_degrades_to_safe_deterministic_actions(settings):
    report = go(settings, llm=ScriptedLLM(default="I think everything is fine!"))

    by_id = {o.task_id: o for o in report.outcomes}
    # Without a trusted reading nothing is routed to the warehouse on a guess:
    # the owner is asked, or the task is escalated on dates alone.
    assert (by_id["TC-1002"].decision, by_id["TC-1002"].target) == (
        "follow_up_owner",
        "Maria Cruz",
    )
    assert by_id["TC-1008"].decision == "escalate"
    assert by_id["TC-1008"].reason == "Overdue by 6 days, escalation threshold is 5 days"
    assert {o.drafted_by for o in report.outcomes if o.message} == {"template"}
    assert report.exit_code == 0
    assert report.llm.rejected_answers > 0


def test_records_the_api_cannot_identify_are_reported_not_processed(settings, tmp_path):
    records = json.loads(FIXTURE.read_text()) + [{"task": "No id"}, "junk"]
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(records))

    report = go(replace(settings, tasks_file=path))

    assert len(report.outcomes) == 10
    assert len(report.rejected_records) == 2


def test_audit_log_records_every_decision(settings):
    report = go(settings)

    events = [
        json.loads(line) for line in (settings.out_dir / "audit.jsonl").read_text().splitlines()
    ]
    assert {e["run_id"] for e in events} == {report.run_id}
    assert sum(e["event"] == "decision" for e in events) == 10
    assert events[-1]["event"] == "run_finished"
    assert events[-1]["deliveries"] == {"sent": 7}
