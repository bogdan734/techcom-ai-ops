import json
import shutil
import sqlite3
from contextlib import closing
from datetime import date, timedelta

import pytest

from conftest import make_task, raw_task
from followup.models import Action, Decision
from followup.notes import reading_key
from followup.store import Store
from followup.team import split as split_module
from followup.team.org import UNROUTED, Org
from followup.team.routes import pin_routes
from followup.team.schema import CONTROL
from followup.team.split import check_legacy, logical_digest, split
from followup.team.vault import Vault, VaultError
from teamkit import make_vault, sample_people

TODAY = date(2026, 10, 21)
ORG = Org.from_records(sample_people())
RECORDS = [
    raw_task(task_id="TC-1", assigned_to="Eve Engineer", notes="Waiting for materials."),
    raw_task(task_id="TC-2", assigned_to="Finn Field"),
    raw_task(task_id="TC-3", assigned_to=None),
]


def action(task_id, status="pending", sent=False):
    return (
        Action(f"id-{task_id}", task_id, Decision.FOLLOW_UP_OWNER, "someone", "r", "m", "template"),
        status,
        sent,
    )


def legacy(out):
    """What the submitted version leaves behind: actions and one reading, in one file."""
    out.mkdir(parents=True, exist_ok=True)
    store = Store(out / "state.db", TODAY)
    for task_id in ("TC-1", "TC-2", "TC-3", "TC-GONE"):
        stored = store.claim(action(task_id)[0])
        store.start_attempt(stored.action.action_id)
        store.start_attempt(stored.action.action_id)
        store.mark_sent(stored.action.action_id, f"msg-{task_id}")
    store.keep_reading(
        reading_key(make_task(task_id="TC-1", notes="Waiting for materials.")),
        {"blocker": "materials", "contains_instructions": False},
        "stub",
    )
    store.keep_reading("orphan-key", {"blocker": "none", "contains_instructions": False}, "stub")
    store.close()
    return out / "state.db"


def rows(path, table="actions"):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()


def all_actions(out):
    found = []
    for path in sorted((out / "state").glob("[!.]*.db")):
        if ".before-" not in path.name and path.stem != "_control":
            found += rows(path)
    return sorted(found)


def test_split_keeps_every_action_with_status_attempts_and_message_id(tmp_path):
    source = legacy(tmp_path)
    report = split(tmp_path, RECORDS, ORG, "hidden", TODAY)
    assert all_actions(tmp_path) == rows(source)  # the union equals the source, column for column
    assert sum(report.actions.values()) == 4 and sum(report.readings.values()) == 2
    assert report.actions == {"engineering": 1, "field-ops": 1, UNROUTED: 2}


def test_split_pins_the_routes_the_runner_uses(tmp_path):
    legacy(tmp_path)
    split(tmp_path, RECORDS, ORG, "hidden", TODAY)
    vault = Vault(tmp_path / "state", ORG, "hidden")
    control = vault.open(CONTROL)
    tasks = [make_task(task_id="TC-1", assigned_to="Eve Engineer")]
    # an owner who has since moved to another department does not move the task
    moved = Org.from_records(
        [{**p, "department": "field-ops"} if p["id"] == "eve" else p for p in sample_people()]
    )
    assert pin_routes(control, tasks, moved, TODAY + timedelta(days=9))["TC-1"] == "engineering"


def test_unknown_task_actions_go_to_unrouted(tmp_path):
    legacy(tmp_path)
    split(tmp_path, RECORDS, ORG, "hidden", TODAY)
    ids = [r[0] for r in rows(tmp_path / "state" / f"{UNROUTED}.db")]
    assert ids == ["id-TC-3", "id-TC-GONE"]
    assert [r[0] for r in rows(tmp_path / "state" / f"{UNROUTED}.db", "note_readings")] == [
        "orphan-key"
    ]


def test_a_reading_follows_its_task_to_the_department(tmp_path):
    legacy(tmp_path)
    split(tmp_path, RECORDS, ORG, "hidden", TODAY)
    assert len(rows(tmp_path / "state" / "engineering.db", "note_readings")) == 1


def test_crash_mid_split_leaves_no_state_dir(tmp_path, monkeypatch):
    legacy(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(split_module, "_copy", boom)
    with pytest.raises(RuntimeError):
        split(tmp_path, RECORDS, ORG, "hidden", TODAY)
    assert not (tmp_path / "state").exists() and not (tmp_path / "state.split-tmp").exists()
    monkeypatch.undo()
    split(tmp_path, RECORDS, ORG, "hidden", TODAY)  # and it can be done afterwards


def test_split_refuses_an_existing_state_dir(tmp_path):
    legacy(tmp_path)
    split(tmp_path, RECORDS, ORG, "hidden", TODAY)
    with pytest.raises(VaultError, match="already exists"):
        split(tmp_path, RECORDS, ORG, "hidden", TODAY)


def test_split_never_writes_state_db(tmp_path):
    source = legacy(tmp_path)
    before = rows(source), logical_digest(sqlite3.connect(source))
    split(tmp_path, RECORDS, ORG, "hidden", TODAY)
    assert (rows(source), logical_digest(sqlite3.connect(source))) == before


def test_a_missing_state_db_cannot_be_split(tmp_path):
    with pytest.raises(VaultError, match="nothing to split"):
        split(tmp_path, RECORDS, ORG, "hidden", TODAY)


def test_unsplit_state_with_actions_is_refused(tmp_path):
    legacy(tmp_path)
    with pytest.raises(VaultError, match="never split"):
        check_legacy(tmp_path, Vault(tmp_path / "state", ORG, "hidden"))


def test_no_state_db_or_an_empty_one_is_fine(tmp_path):
    check_legacy(tmp_path, Vault(tmp_path / "state", ORG, "hidden"))
    Store(tmp_path / "state.db").close()
    check_legacy(tmp_path, Vault(tmp_path / "state", ORG, "hidden"))


def test_state_db_written_after_split_is_refused(tmp_path):
    source = legacy(tmp_path)
    split(tmp_path, RECORDS, ORG, "hidden", TODAY)
    vault = Vault(tmp_path / "state", ORG, "hidden")
    check_legacy(tmp_path, vault)  # unchanged: fine
    store = Store(source, TODAY)
    store.claim(action("TC-9")[0])  # an old version still running in the directory
    store.close()
    with pytest.raises(VaultError, match="changed after the split"):
        check_legacy(tmp_path, vault)


def test_split_result_is_a_valid_vault(tmp_path):
    legacy(tmp_path)
    split(tmp_path, RECORDS, ORG, "hidden", TODAY)
    vault = Vault(tmp_path / "state", ORG, "hidden")
    assert set(vault.names()) == set(vault.known())
    assert all(not vault.check(name).pending for name in vault.names())
    assert json.loads(json.dumps(sorted(vault.names())))
    shutil.rmtree(tmp_path / "state")
    assert make_vault  # imported for symmetry with the other team tests
