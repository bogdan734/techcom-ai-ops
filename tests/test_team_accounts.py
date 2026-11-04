import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from followup.team.accounts import (
    LOCK_AFTER,
    AuthError,
    accounts,
    end_session,
    remove,
    session_person,
    set_password,
    start_session,
    verify,
)
from followup.team.schema import CONTROL_STEPS

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
TODAY = date(2026, 10, 21)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:", isolation_level=None)
    for step in CONTROL_STEPS:
        conn.executescript(step.sql)
    set_password(conn, "eve", "correct horse", TODAY)
    return conn


def test_the_right_password_signs_in_and_a_wrong_one_does_not(db):
    assert verify(db, "eve", "correct horse", NOW) is True
    assert verify(db, "eve", "wrong horse", NOW) is False
    assert verify(db, "nobody", "correct horse", NOW) is False


def test_only_a_salted_hash_is_stored(db):
    salt, digest = db.execute("SELECT salt, hash FROM accounts").fetchone()
    assert "correct horse" not in salt + digest and len(digest) == 64
    set_password(db, "max", "correct horse", TODAY)  # the same password, another salt
    assert db.execute("SELECT count(DISTINCT hash) FROM accounts").fetchone() == (2,)


def test_a_short_password_is_refused(db):
    with pytest.raises(AuthError, match="at least 8"):
        set_password(db, "max", "short", TODAY)


def test_five_wrong_passwords_lock_the_account_even_for_the_right_one(db):
    for _ in range(LOCK_AFTER):
        assert verify(db, "eve", "nope nope", NOW) is False
    assert verify(db, "eve", "correct horse", NOW) is False  # locked
    assert verify(db, "eve", "correct horse", NOW + timedelta(minutes=6)) is True  # lock expired


def test_a_success_resets_the_count(db):
    for _ in range(LOCK_AFTER - 1):
        verify(db, "eve", "nope nope", NOW)
    assert verify(db, "eve", "correct horse", NOW) is True
    for _ in range(LOCK_AFTER - 1):
        verify(db, "eve", "nope nope", NOW)
    assert verify(db, "eve", "correct horse", NOW) is True


def test_a_session_lasts_eight_hours_and_ends_on_sign_out(db):
    token = start_session(db, "eve", NOW)
    assert session_person(db, token, NOW + timedelta(hours=7)) == "eve"
    assert session_person(db, token, NOW + timedelta(hours=9)) is None
    assert session_person(db, "", NOW) is None and session_person(db, "forged", NOW) is None
    end_session(db, token)
    assert session_person(db, token, NOW) is None


def test_the_token_itself_is_not_stored(db):
    token = start_session(db, "eve", NOW)
    assert token not in db.execute("SELECT token_hash FROM sessions").fetchone()[0]


def test_removing_an_account_ends_its_sessions(db):
    token = start_session(db, "eve", NOW)
    assert remove(db, "eve") is True and accounts(db) == []
    assert session_person(db, token, NOW) is None
    assert remove(db, "eve") is False
