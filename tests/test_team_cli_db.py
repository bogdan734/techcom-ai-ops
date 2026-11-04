import shutil

import pytest

from conftest import FIXTURE
from followup.cli import main
from followup.team import vault as vault_module
from followup.team.schema import CONTROL, CONTROL_STEPS, DEPARTMENT_STEPS, Step
from teamkit import run_day, world_records, write_org


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("ORG_FILE", "TASKS_FILE", "OUT_DIR", "EVALUATION_DATE", "TRUST_VISIBILITY"):
        monkeypatch.delenv(name, raising=False)


def db(tmp_path, *args, tasks=None):
    return main(
        [
            "team",
            "db",
            *args,
            "--org",
            str(tmp_path / "cfg" / "org.json"),
            "--tasks",
            str(tasks or tmp_path / "tasks.json"),
            "--out",
            str(tmp_path / "out"),
            "--today",
            "2026-10-22",
        ]
    )


def test_status_on_a_fresh_dir_creates_nothing(tmp_path, capsys):
    run_day(tmp_path, world_records())  # writes the org file and the tasks; then start over
    shutil.rmtree(tmp_path / "out")
    assert db(tmp_path, "status") == 0
    assert "missing" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


def test_status_lists_steps_and_counts(tmp_path, capsys):
    run_day(tmp_path, world_records())
    run_day(tmp_path, world_records("Done"), "2026-10-22")
    assert db(tmp_path, "status") == 0
    out = capsys.readouterr().out
    assert "engineering" in out and "steps 2" in out and "quizzes 1" in out and "_control" in out


def test_verify_is_clean_then_names_a_swapped_file(tmp_path, capsys):
    run_day(tmp_path, world_records())
    assert db(tmp_path, "verify") == 0
    state = tmp_path / "out" / "state"
    shutil.copy(state / "engineering.db", state / "field-ops.db")
    assert db(tmp_path, "verify") == 1
    assert "belongs to 'engineering'" in capsys.readouterr().err


def test_migrate_prints_the_backup_path(tmp_path, capsys, monkeypatch):
    run_day(tmp_path, world_records())
    later = Step("003-later", "CREATE TABLE later (x INTEGER);")
    monkeypatch.setattr(
        vault_module,
        "steps_for",
        lambda name: CONTROL_STEPS if name == CONTROL else (*DEPARTMENT_STEPS, later),
    )
    assert db(tmp_path, "verify") == 1  # pending steps are a problem until they are applied
    capsys.readouterr()
    assert db(tmp_path, "migrate") == 0
    out = capsys.readouterr().out
    assert "engineering: applied 003-later; backup" in out and "before-003-later.db" in out
    assert (tmp_path / "out" / "state" / "engineering.before-003-later.db").exists()
    assert db(tmp_path, "verify") == 0
    assert db(tmp_path, "migrate") == 0 and "nothing to migrate" in capsys.readouterr().out


def test_split_moves_the_old_state_and_a_second_split_exits_1(tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["run", "--tasks", str(FIXTURE), "--out", str(out), "--today", "2026-10-21"]) == 0
    (tmp_path / "cfg").mkdir()
    write_org(tmp_path / "cfg")  # the org the team will use; no team run has happened yet
    capsys.readouterr()
    assert db(tmp_path, "split", tasks=FIXTURE) == 0
    text = capsys.readouterr().out
    assert "split state.db" in text and "actions:" in text and "unrouted" in text
    assert db(tmp_path, "split", tasks=FIXTURE) == 1
    assert "already exists" in capsys.readouterr().err
    assert (out / "state.db").exists()  # the old file stays as the record of the old version
