import sqlite3

import pytest

from teamkit import make_vault

TRUST_ROW = "INSERT INTO trust_events VALUES ('eve', 'TC-1', 'quiz', 1, '2026-10-21')"
QUIZ_ROW = (
    "INSERT INTO quizzes VALUES ('q1', 'TC-1', 'eve', 'max', 'h', 'tightened', '[]', '2026-10-21')"
)


@pytest.fixture
def vault(tmp_path):
    return make_vault(tmp_path)


def as_person(vault, pid, name, writes=frozenset()):
    return vault.open(name, vault.org.person(pid), writes)


def test_engineer_cannot_open_control(vault):
    with pytest.raises(PermissionError, match="managers and up"):
        as_person(vault, "eve", "_control")


def test_unrouted_needs_a_manager(vault):
    with pytest.raises(PermissionError):
        as_person(vault, "finn", "_unrouted")
    as_person(vault, "max", "_unrouted").close()


def test_staff_cannot_open_another_department(vault):
    with pytest.raises(PermissionError, match="may not open"):
        as_person(vault, "eve", "field-ops")
    as_person(vault, "eve", "engineering").close()


def test_attach_denied_for_every_viewer(vault, tmp_path):
    other = tmp_path / "other.db"
    sqlite3.connect(other).close()
    viewers = [None, "eve", "max", "bea"]
    for pid in viewers:
        db = vault.open("engineering", vault.org.person(pid) if pid else None)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized|prohibited"):
            db.execute(f"ATTACH DATABASE '{other}' AS stolen")
        db.close()


def test_ddl_and_pragma_are_denied(vault):
    for pid in (None, "bea"):
        db = vault.open("engineering", vault.org.person(pid) if pid else None)
        for statement in ("CREATE TABLE x (y)", "DROP TABLE threads", "PRAGMA writable_schema=1"):
            with pytest.raises(sqlite3.DatabaseError):
                db.execute(statement)
        db.close()


def test_read_only_grant_cannot_insert(vault):
    db = as_person(vault, "max", "engineering")
    insert = "INSERT INTO reviews VALUES ('q1', 'max', 1, 'ok', '2026-10-21')"
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        db.execute(insert)
    db.set_authorizer(None)  # with the authorizer gone, the connection itself is read-only
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.execute(insert)


def test_writes_open_a_writable_connection_for_those_tables_only(vault):
    db = as_person(vault, "max", "engineering", frozenset({"reviews"}))
    db.execute("INSERT INTO reviews VALUES ('q1', 'max', 1, 'ok', '2026-10-21')")
    with pytest.raises(sqlite3.DatabaseError, match="not authorized|prohibited"):
        db.execute("DELETE FROM quizzes")


def test_staff_can_write_answers_but_not_reviews(vault):
    as_person(vault, "eve", "engineering", frozenset({"answers"})).close()
    with pytest.raises(PermissionError, match="reviews"):
        as_person(vault, "eve", "engineering", frozenset({"reviews"}))


def test_staff_cannot_read_grades_or_the_outbox(vault):
    db = as_person(vault, "eve", "engineering")
    for table in ("grades", "reviews", "actions", "sqlite_master"):
        with pytest.raises(sqlite3.DatabaseError, match="not authorized|prohibited"):
            db.execute(f"SELECT * FROM {table}").fetchall()


def test_issuers_read_grades_in_another_department(vault):
    db = as_person(vault, "max", "engineering")
    assert db.execute("SELECT * FROM grades").fetchall() == []


def test_hidden_visibility_denies_trust_to_the_boss(tmp_path):
    hidden = make_vault(tmp_path / "h", visibility="hidden")
    db = as_person(hidden, "bea", "_control")
    with pytest.raises(sqlite3.DatabaseError, match="not authorized|prohibited"):
        db.execute("SELECT * FROM trust_events").fetchall()
    assert db.execute("SELECT count(*) FROM task_routes").fetchone() == (0,)


def test_issuers_visibility_shows_trust_to_issuers_only(vault):
    service = vault.open("_control")
    service.execute(TRUST_ROW)
    service.close()
    assert as_person(vault, "bea", "_control").execute("SELECT * FROM trust_events").fetchall()
    with pytest.raises(PermissionError):
        as_person(vault, "eve", "_control")


def test_the_level_column_reads_null_without_the_right_to_see_trust(vault):
    service = vault.open("engineering")
    service.execute(QUIZ_ROW)
    service.close()
    assert as_person(vault, "eve", "engineering").execute(
        "SELECT level FROM quizzes"
    ).fetchall() == [(None,)]
    assert as_person(vault, "max", "engineering").execute(
        "SELECT level FROM quizzes"
    ).fetchall() == [("tightened",)]


def test_hidden_visibility_hides_the_level_from_issuers_too(tmp_path):
    hidden = make_vault(tmp_path / "h", visibility="hidden")
    service = hidden.open("engineering")
    service.execute(QUIZ_ROW)
    service.close()
    rows = as_person(hidden, "bea", "engineering").execute("SELECT level FROM quizzes").fetchall()
    assert rows == [(None,)]


def test_support_is_open_to_anyone_for_chat_tables_only(vault):
    db = as_person(vault, "finn", "support", frozenset({"threads"}))
    assert db.execute("SELECT count(*) FROM threads").fetchone() == (0,)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized|prohibited"):
        db.execute("SELECT * FROM actions").fetchall()


def test_the_service_reads_and_writes_everything(vault):
    db = vault.open("engineering")
    db.execute(QUIZ_ROW)
    assert db.execute("SELECT count(*) FROM actions").fetchone() == (0,)
