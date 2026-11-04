import json
from pathlib import Path

import pytest

from conftest import FIXTURE, raw_task
from followup.config import ConfigError, Settings
from followup.pipeline import run
from followup.run_lock import RunBusy, hold_run_lock
from teamkit import write_org

SAMPLE = Path(__file__).resolve().parents[1] / "docs" / "sample" / "decisions.json"
SPEC = "Install four handholes. The crew must record GPS coordinates."


def base_settings(tmp_path, **env):
    return Settings.from_env(
        {
            "TASKS_FILE": str(FIXTURE),
            "OUT_DIR": str(tmp_path / "out"),
            "EVALUATION_DATE": "2026-10-21",
            **env,
        }
    )


def team_settings(tmp_path, records, today="2026-10-21", **env):
    fixture = tmp_path / "tasks.json"
    fixture.write_text(json.dumps(records))
    org = write_org(tmp_path)
    return Settings.from_env(
        {
            "TASKS_FILE": str(fixture),
            "OUT_DIR": str(tmp_path / "out"),
            "EVALUATION_DATE": today,
            "ORG_FILE": str(org),
            "CALL_TIME": "10:00",
            **env,
        }
    )


def team_records(status="Working on it"):
    return [
        raw_task(
            task_id="TC-1", assigned_to="Eve Engineer", status=status, spec=SPEC, assigned_by="max"
        ),
        raw_task(
            task_id="TC-2",
            assigned_to="Finn Field",
            status="Blocked",
            last_update="2026-10-15",
            notes="Awaiting materials from warehouse; delivery date unknown.",
            due_date="2026-10-18",
        ),
    ]


def test_without_org_decisions_equal_the_submitted_sample(tmp_path):
    report = run(base_settings(tmp_path))
    assert (tmp_path / "out" / "decisions.json").read_bytes() == SAMPLE.read_bytes()
    assert report.team is None and (tmp_path / "out" / "state.db").exists()
    assert (
        not (tmp_path / "out" / "state").exists() and not (tmp_path / "out" / "team.json").exists()
    )


def test_busy_lock_raises_and_writes_no_audit_line(tmp_path):
    out = tmp_path / "out"
    with hold_run_lock(out), pytest.raises(RunBusy):
        run(base_settings(tmp_path))
    assert not (out / "audit.jsonl").exists() and not (out / "delivery_log.jsonl").exists()


def test_a_finished_run_releases_the_lock(tmp_path):
    run(base_settings(tmp_path))
    assert run(base_settings(tmp_path)).exit_code == 0


def test_run_lock_false_skips_the_lock(tmp_path):
    with hold_run_lock(tmp_path / "out"):
        assert run(base_settings(tmp_path, RUN_LOCK="false")).exit_code == 0


def test_team_dry_run_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="dry-run"):
        run(team_settings(tmp_path, team_records()), dry_run=True)
    assert not (tmp_path / "out").exists()


def test_a_team_run_writes_the_same_decisions_and_the_team_artefact(tmp_path):
    report = run(team_settings(tmp_path, team_records()))
    out = tmp_path / "out"
    assert report.exit_code == 0 and report.team.replay == "new"
    assert (out / "team.json").exists() and (out / "decisions.json").exists()
    assert not (out / "state.db").exists() and (out / "state" / "engineering.db").exists()
    decisions = json.loads((out / "decisions.json").read_text())
    assert [d["task_id"] for d in decisions] == ["TC-1", "TC-2"]
    assert next(d for d in decisions if d["task_id"] == "TC-2")["delivery"] == "sent"


def test_a_repeated_team_run_is_a_replay_and_sends_nothing(tmp_path):
    settings = team_settings(tmp_path, team_records())
    run(settings)
    log = (tmp_path / "out" / "delivery_log.jsonl").read_text()
    again = run(settings)
    assert again.team.replay == "replay" and again.exit_code == 0
    assert (tmp_path / "out" / "delivery_log.jsonl").read_text() == log


def test_team_exit_code_2_when_undelivered(tmp_path):
    run(team_settings(tmp_path, team_records()))
    closed = team_settings(tmp_path, team_records("Done"), today="2026-10-22")
    report = run(closed, {"send": ["503"] * 80})
    assert report.team.undelivered > 0 and report.exit_code == 2
    assert run(closed).exit_code == 0


def test_a_mismatch_makes_the_run_exit_1(tmp_path):
    import sqlite3
    from contextlib import closing

    settings = team_settings(tmp_path, team_records())
    run(settings)
    with closing(sqlite3.connect(tmp_path / "out" / "state" / "_control.db")) as db:
        db.execute("UPDATE run_ledger SET artefact_digest = 'tampered'")
        db.commit()
    report = run(settings)
    assert report.team.replay == "mismatch" and report.exit_code == 1


def test_an_unreachable_tasks_api_aborts_a_team_run_cleanly(tmp_path):
    report = run(team_settings(tmp_path, team_records()), {"tasks": ["503"] * 20})
    assert report.aborted and report.exit_code == 1 and report.team is None
