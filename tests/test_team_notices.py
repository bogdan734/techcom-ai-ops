import sqlite3
from datetime import date, timedelta

import pytest

from conftest import make_task
from followup.models import Action, Decision
from followup.store import Store
from followup.team.extras import TaskExtras
from followup.team.notices import (
    _REQUIRED,
    Channel,
    Kind,
    Message,
    begin_activation,
    fresh,
    history_for,
    lifecycle_notices,
    record_history,
    render,
    to_action,
)
from followup.team.org import Org
from followup.team.schema import DEPARTMENT_STEPS
from teamkit import sample_people

TODAY = date(2026, 10, 21)
ORG = Org.from_records(sample_people())
SPEC = "Install four handholes. The crew must record GPS coordinates."


def memory():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    for step in DEPARTMENT_STEPS:
        db.executescript(step.sql)
    return db


def extras(spec=SPEC, created=None, by="max"):
    return TaskExtras("TC-2001", spec, by, None, created)


def task(**kw):
    return make_task(assigned_to="Eve Engineer", **kw)


def message(**kw):
    base = {
        "kind": Kind.TASK_CLOSED,
        "task_id": "TC-1",
        "recipient": "max",
        "channel": Channel.MESSAGE,
        "state_key": "done",
        "facts": {"assignee": "Eve", "task_id": "TC-1", "title": "T", "check": ""},
    }
    return Message(**{**base, **kw})


def run(db, current, ex, today, activation=False, others=(), window=2):
    record_history(db, current, ex, today, activation)
    rows = history_for(db, current.task_id)
    owners = {"Eve Engineer": list(others)}
    return lifecycle_notices(current, ex, rows, owners, ORG, today, window)


def kinds(messages):
    return [(m.kind.value, m.recipient) for m in messages]


def test_id_ignores_facts_and_wording():
    first = message()
    reworded = message(facts={**first.facts, "check": " Different words entirely."})
    assert first.message_id == reworded.message_id and len(first.message_id) == 32
    for change in (
        {"kind": Kind.NEXT_TASK},
        {"task_id": "TC-2"},
        {"recipient": "sid"},
        {"channel": Channel.CALL},
        {"state_key": "other"},
    ):
        facts = {"next": "n", "task_id": "TC-1", "title": "T", "assignee": "E", "check": ""}
        assert message(facts=facts, **change).message_id != first.message_id


def test_a_notice_missing_a_fact_fails_loudly():
    with pytest.raises(KeyError, match="check"):
        message(facts={"assignee": "Eve", "task_id": "TC-1", "title": "T"})


def test_every_kind_renders_from_its_own_facts():
    for kind, names in _REQUIRED.items():
        text = render(Message(kind, "TC-1", "eve", Channel.MESSAGE, "k", dict.fromkeys(names, "x")))
        assert text and "{" not in text


def test_activation_does_not_announce_old_tasks():
    db = memory()
    assert begin_activation(db, TODAY) is True and begin_activation(db, TODAY) is False
    assert run(db, task(), extras(), TODAY, activation=True) == []


def test_created_inside_window_is_announced():
    db = memory()
    fresh_task = run(db, task(), extras(created=TODAY - timedelta(days=1)), TODAY, activation=True)
    assert kinds(fresh_task) == [("new_task", "eve")]
    assert "Issued by Max Manager" in render(fresh_task[0]) and "must record GPS" in render(
        fresh_task[0]
    )


def test_created_outside_window_is_not():
    db = memory()
    assert run(db, task(), extras(created=TODAY - timedelta(days=5)), TODAY, activation=True) == []


def test_first_seen_after_activation_is_new():
    db = memory()
    assert kinds(run(db, task(), extras(), TODAY, activation=False)) == [("new_task", "eve")]
    # three days later it is no longer news, and the id would have suppressed it anyway
    assert run(db, task(), extras(), TODAY + timedelta(days=3)) == []


def test_a_task_first_seen_done_is_closed_not_new():
    out = run(memory(), task(status="Done"), extras(), TODAY)
    assert sorted(kinds(out)) == [("next_task", "eve"), ("task_closed", "max")]


def test_unknown_owner_gets_nothing():
    stranger = make_task(assigned_to="Nobody Known")
    assert run(memory(), stranger, None, TODAY) == []


def test_spec_change_tells_assignee_and_issuer():
    db = memory()
    run(db, task(), extras(), TODAY, activation=True)
    changed = extras(SPEC + " Label every handhole.")
    out = run(db, task(), changed, TODAY + timedelta(days=1))
    assert sorted(kinds(out)) == [("spec_changed", "eve"), ("spec_changed", "max")]
    assert "Added: Label every handhole." in render(out[0]) or "Added: Label" in render(out[1])
    assert out[0].state_key == out[1].state_key


def test_close_tells_issuer_and_names_next_open_task():
    db = memory()
    run(db, task(), extras(), TODAY, activation=True)
    later = [
        make_task(
            task_id="TC-3", assigned_to="Eve Engineer", due_date="2026-10-25", status="Not Started"
        ),
        make_task(
            task_id="TC-2",
            assigned_to="Eve Engineer",
            due_date="2026-10-30",
            status="Working on it",
        ),
        make_task(
            task_id="TC-1",
            assigned_to="Eve Engineer",
            due_date="2026-11-02",
            status="Working on it",
        ),
        make_task(task_id="TC-0", assigned_to="Eve Engineer", status="Done"),
    ]
    done = task(status="Done", last_update="2026-10-22")
    out = run(db, done, extras(), TODAY + timedelta(days=1), others=later)
    assert sorted(kinds(out)) == [("next_task", "eve"), ("task_closed", "max")]
    text = render(next(m for m in out if m.kind is Kind.NEXT_TASK))
    assert "TC-3" in text and "due 2026-10-25" in text  # earliest due date, not lowest id


def test_next_task_with_a_tie_takes_the_lowest_id_and_no_date_goes_last():
    db = memory()
    run(db, task(), extras(), TODAY, activation=True)
    others = [
        make_task(task_id="TC-9", assigned_to="Eve Engineer", due_date=None),
        make_task(task_id="TC-8", assigned_to="Eve Engineer", due_date="2026-11-01"),
        make_task(task_id="TC-7", assigned_to="Eve Engineer", due_date="2026-11-01"),
    ]
    out = run(db, task(status="Done"), extras(), TODAY + timedelta(days=1), others=others)
    assert "TC-7" in render(next(m for m in out if m.kind is Kind.NEXT_TASK))


def test_close_without_other_tasks_says_so():
    db = memory()
    run(db, task(), extras(), TODAY, activation=True)
    out = run(db, task(status="Done"), extras(), TODAY + timedelta(days=1))
    assert "no other open tasks" in render(next(m for m in out if m.kind is Kind.NEXT_TASK))


def test_a_task_done_on_activation_is_not_a_closing_event():
    db = memory()
    assert run(db, task(status="Done"), extras(), TODAY, activation=True) == []


def test_issuer_falls_back_to_the_owners_manager():
    db = memory()
    run(db, task(), None, TODAY, activation=True)
    out = run(db, task(status="Done"), None, TODAY + timedelta(days=1))
    assert ("task_closed", "max") in kinds(out) and "follows" not in render(out[0])


def test_same_day_rerun_same_rows_and_messages():
    db = memory()
    first = run(db, task(), extras(), TODAY)
    assert record_history(db, task(), extras(), TODAY, False) == []  # nothing new to record
    assert run(db, task(), extras(), TODAY) == first
    assert [m.message_id for m in first] == [m.message_id for m in run(db, task(), extras(), TODAY)]


def test_fresh_prefers_the_creation_date():
    db = memory()
    record_history(db, task(), extras(), TODAY, True)
    (row, *_) = history_for(db, "TC-2001")
    assert fresh(row, TODAY, TODAY, 2) and not fresh(row, None, TODAY, 2)
    assert not fresh(row, TODAY + timedelta(days=1), TODAY, 2)


def test_notify_round_trips_through_store_and_old_values_unchanged(tmp_path):
    assert {d.value for d in Decision} - {"notify"} == {
        "no_action",
        "follow_up_owner",
        "request_eta",
        "clarify_missing_info",
        "escalate",
    }
    action = to_action(message(), "Max Manager")
    assert isinstance(action, Action) and action.decision is Decision.NOTIFY
    assert action.reason == "team: task_closed" and action.drafted_by == "template"
    store = Store(tmp_path / "s.db")
    stored = store.claim(action)
    assert stored.action == action and store.get_action(action.action_id).action == action
