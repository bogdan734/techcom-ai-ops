import shutil
import sqlite3
from contextlib import closing

import pytest

from followup.store import Store
from followup.team import vault as vault_module
from followup.team.schema import APPLICATION_ID, DEPARTMENT_STEPS, Step
from followup.team.vault import VaultError
from teamkit import make_vault


def dump(path):
    with closing(sqlite3.connect(path)) as db:
        return list(db.iterdump())


def test_create_makes_a_checked_department_file(tmp_path):
    vault = make_vault(tmp_path, names=["engineering"])
    status = vault.check("engineering")
    assert status.applied == ("001-tables", "002-indexes") and status.pending == ()
    with closing(sqlite3.connect(vault.path("engineering"))) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        assert {"actions", "quizzes", "chat_messages", "meta"} <= tables
        assert db.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID


def test_control_file_has_no_base_outbox_tables(tmp_path):
    vault = make_vault(tmp_path, names=["_control"])
    with closing(sqlite3.connect(vault.path("_control"))) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    assert "trust_events" in tables and "actions" not in tables


def test_refuses_a_file_from_another_department(tmp_path):
    vault = make_vault(tmp_path, names=["engineering"])
    shutil.copy(vault.path("engineering"), vault.path("management"))
    with pytest.raises(VaultError, match="belongs to 'engineering'"):
        vault.check("management")


def test_refuses_a_plain_state_db(tmp_path):
    vault = make_vault(tmp_path, names=[])
    Store(vault.path("engineering")).close()  # the submitted version's single database
    with pytest.raises(VaultError, match="not a team database"):
        vault.check("engineering")


def test_checksum_drift_is_an_error(tmp_path, monkeypatch):
    vault = make_vault(tmp_path, names=["engineering"])
    edited = Step(DEPARTMENT_STEPS[0].name, DEPARTMENT_STEPS[0].sql + "\nCREATE TABLE sneaky (x);")
    monkeypatch.setattr(vault_module, "steps_for", lambda name: (edited, DEPARTMENT_STEPS[1]))
    with pytest.raises(VaultError, match="applied as something else"):
        vault.check("engineering")


def test_reindenting_a_step_is_not_drift(tmp_path, monkeypatch):
    vault = make_vault(tmp_path, names=["engineering"])
    spaced = Step(DEPARTMENT_STEPS[0].name, DEPARTMENT_STEPS[0].sql.replace("    ", "\t"))
    monkeypatch.setattr(vault_module, "steps_for", lambda name: (spaced, DEPARTMENT_STEPS[1]))
    assert vault.check("engineering").pending == ()


def test_newer_ledger_is_an_error(tmp_path, monkeypatch):
    vault = make_vault(tmp_path, names=["engineering"])
    monkeypatch.setattr(vault_module, "steps_for", lambda name: DEPARTMENT_STEPS[:1])
    with pytest.raises(VaultError, match="newer than this code"):
        vault.check("engineering")


def test_a_newer_base_schema_is_an_error(tmp_path):
    vault = make_vault(tmp_path, names=["engineering"])
    with closing(sqlite3.connect(vault.path("engineering"))) as db:
        db.execute("PRAGMA user_version = 99")
    with pytest.raises(VaultError, match="base schema 99"):
        vault.check("engineering")


def _old_vault(tmp_path, monkeypatch):
    """A department created when only step 1 existed, then code that adds ``later``."""
    monkeypatch.setattr(vault_module, "steps_for", lambda name: DEPARTMENT_STEPS[:1])
    vault = make_vault(tmp_path, names=["engineering"])
    return vault


def test_migrate_applies_pending_steps_in_order(tmp_path, monkeypatch):
    vault = _old_vault(tmp_path, monkeypatch)
    later = Step("003-extra", "CREATE TABLE extra (x INTEGER);")
    steps = (*DEPARTMENT_STEPS, later)
    monkeypatch.setattr(vault_module, "steps_for", lambda name: steps)
    assert vault.check("engineering").pending == ("002-indexes", "003-extra")
    assert vault.migrate("engineering") == ("002-indexes", "003-extra")
    assert vault.check("engineering").pending == ()
    assert vault.migrate("engineering") == ()


def test_failed_step_rolls_back_and_keeps_backup(tmp_path, monkeypatch):
    vault = _old_vault(tmp_path, monkeypatch)
    before = dump(vault.path("engineering"))
    bad = Step("002-bad", "CREATE TABLE half (x INTEGER); INSERT INTO missing VALUES (1);")
    monkeypatch.setattr(vault_module, "steps_for", lambda name: (DEPARTMENT_STEPS[0], bad))
    with pytest.raises(VaultError, match="rolled back"):
        vault.migrate("engineering")
    assert dump(vault.path("engineering")) == before  # no half table, no ledger row
    backup = vault.path("engineering").with_name("engineering.before-002-bad.db")
    assert dump(backup) == before


def test_backup_name_is_stable_on_rerun(tmp_path, monkeypatch):
    vault = _old_vault(tmp_path, monkeypatch)
    bad = Step("002-bad", "INSERT INTO missing VALUES (1);")
    monkeypatch.setattr(vault_module, "steps_for", lambda name: (DEPARTMENT_STEPS[0], bad))
    for _ in range(2):
        with pytest.raises(VaultError):
            vault.migrate("engineering")
    backups = sorted(p.name for p in vault.root.glob("*.before-*"))
    assert backups == ["engineering.before-002-bad.db"]


def test_open_never_creates_a_file(tmp_path):
    vault = make_vault(tmp_path, names=[])
    with pytest.raises(VaultError, match="does not exist"):
        vault.open("engineering")
    assert list(vault.root.glob("*")) == []


def test_open_refuses_pending_steps(tmp_path, monkeypatch):
    vault = _old_vault(tmp_path, monkeypatch)
    monkeypatch.setattr(vault_module, "steps_for", lambda name: DEPARTMENT_STEPS)
    with pytest.raises(VaultError, match="pending steps"):
        vault.open("engineering")
    with pytest.raises(VaultError, match="pending steps"):
        vault.ensure("engineering")


def test_create_refuses_an_existing_file(tmp_path):
    vault = make_vault(tmp_path, names=["engineering"])
    with pytest.raises(VaultError, match="already exists"):
        vault.create("engineering")


def test_a_name_outside_the_org_is_refused(tmp_path):
    vault = make_vault(tmp_path, names=[])
    for name in ("../escape", "nowhere", "engineering/../x"):
        with pytest.raises(VaultError, match="not a department"):
            vault.path(name)


def test_crash_during_create_leaves_nothing(tmp_path, monkeypatch):
    broken = Step("001-bad", "CREATE TABLE ok (x); SELECT * FROM missing_table;")
    monkeypatch.setattr(vault_module, "steps_for", lambda name: (broken,))
    vault = make_vault(tmp_path, names=[])
    with pytest.raises(VaultError):
        vault.create("engineering")
    assert vault.names() == () and list(vault.root.glob("*")) == []


def test_ensure_creates_missing_and_accepts_current(tmp_path):
    vault = make_vault(tmp_path, names=[])
    vault.ensure("support")
    vault.ensure("support")
    assert vault.names() == ("support",)
