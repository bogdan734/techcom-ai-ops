import sqlite3
from datetime import date

from conftest import raw_task
from followup.config import Settings
from followup.team.ledger import (
    control_inputs_digest,
    file_digest,
    input_digest,
    inputs_digest,
    record_run,
)
from followup.team.schema import CONTROL_STEPS, DEPARTMENT_STEPS
from teamkit import sample_people, team_settings

D0 = date(2026, 10, 21)


def memory(steps):
    db = sqlite3.connect(":memory:", isolation_level=None)
    for step in steps:
        db.executescript(step.sql)
    return db


def test_inputs_digest_changes_with_human_rows_only():
    db = memory(DEPARTMENT_STEPS)
    empty = inputs_digest(db)
    db.execute(
        "INSERT INTO threads VALUES ('t', 'task', 'TC-1', 'eve', 's', 'open', 0, '2026-10-21')"
    )
    db.execute(
        "INSERT INTO chat_messages VALUES ('m1', 't', 'assistant', 'Spec', 'q', 0, '2026-10-21')"
    )
    assert inputs_digest(db) == empty  # the assistant's posts are output, not input
    db.execute(
        "INSERT INTO chat_messages VALUES ('m2', 't', 'eve', 'Question?', NULL, 0, '2026-10-21')"
    )
    asked = inputs_digest(db)
    assert asked != empty
    db.execute("INSERT INTO answers VALUES ('q', 'q1', 'under 0.5', 0, '2026-10-21')")
    assert inputs_digest(db) not in (empty, asked)


def test_control_digest_sees_clearances_and_not_results():
    control = memory(CONTROL_STEPS)
    base = control_inputs_digest(control)
    control.execute("INSERT INTO trust_events VALUES ('eve', 'q', 'quiz', 1, '2026-10-21')")
    assert control_inputs_digest(control) == base
    control.execute(
        "INSERT INTO trust_events VALUES ('eve', 'c', 'review_cleared', 1, '2026-10-21')"
    )
    assert control_inputs_digest(control) != base


def test_input_digest_follows_the_data_the_org_and_the_day(tmp_path):
    settings = Settings.from_env({"EVALUATION_DATE": "2026-10-21"})
    team = team_settings(tmp_path)
    records = [raw_task()]
    base = input_digest(records, team.org, settings, team, {"engineering": "x"})
    assert base == input_digest(records, team.org, settings, team, {"engineering": "x"})
    other_day = Settings.from_env({"EVALUATION_DATE": "2026-10-22"})
    people = sample_people()
    people[4]["name"] = "Fin Field"
    moved = team_settings(tmp_path / "o", people=people)
    variants = [
        input_digest([raw_task(notes="changed")], team.org, settings, team, {"engineering": "x"}),
        input_digest(records, team.org, other_day, team, {"engineering": "x"}),
        input_digest(records, moved.org, settings, moved, {"engineering": "x"}),
        input_digest(records, team.org, settings, team, {"engineering": "y"}),
    ]
    assert len({base, *variants}) == 5


def test_record_run_new_replay_mismatch():
    control = memory(CONTROL_STEPS)
    assert record_run(control, "in", "out", D0) == "new"
    assert record_run(control, "in", "out", D0) == "replay"
    assert record_run(control, "in", "different", D0) == "mismatch"
    assert record_run(control, "in", "out", D0) == "replay"  # the first artefact still stands
    assert record_run(control, "other", "different", D0) == "new"


def test_file_digest_is_the_content(tmp_path):
    (tmp_path / "a").write_text("x")
    (tmp_path / "b").write_text("x")
    (tmp_path / "c").write_text("y")
    assert file_digest(tmp_path / "a") == file_digest(tmp_path / "b") != file_digest(tmp_path / "c")
