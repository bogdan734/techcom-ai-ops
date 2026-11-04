"""Personal accounts: a password per person, a session per sign-in, and no way to be someone else.

Passwords are stored as scrypt hashes with a salt of their own; nothing here can return one. A
wrong password counts against the account and five in a row lock it for a while. An unknown
account costs the same time as a known one, and the answer is the same, so the sign-in does not
say who exists. A session is a random token in an HttpOnly cookie; only its hash is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import date, datetime, timedelta

MIN_PASSWORD = 8
LOCK_AFTER = 5
LOCK_FOR = timedelta(minutes=5)
SESSION_FOR = timedelta(hours=8)
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
_DUMMY_SALT = bytes(16)


class AuthError(Exception):
    """The password or the account is not acceptable."""


def set_password(db: sqlite3.Connection, person_id: str, password: str, today: date) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"a password needs at least {MIN_PASSWORD} characters")
    salt = secrets.token_bytes(16)
    db.execute(
        "INSERT OR REPLACE INTO accounts (person_id, salt, hash, created_on, failed, locked_until) "
        "VALUES (?, ?, ?, ?, 0, NULL)",
        (person_id, salt.hex(), _hash(password, salt), today.isoformat()),
    )


def remove(db: sqlite3.Connection, person_id: str) -> bool:
    db.execute("DELETE FROM sessions WHERE person_id = ?", (person_id,))
    return db.execute("DELETE FROM accounts WHERE person_id = ?", (person_id,)).rowcount == 1


def accounts(db: sqlite3.Connection) -> list[str]:
    return [row[0] for row in db.execute("SELECT person_id FROM accounts ORDER BY person_id")]


def verify(db: sqlite3.Connection, person_id: str, password: str, now: datetime) -> bool:
    row = db.execute(
        "SELECT salt, hash, failed, locked_until FROM accounts WHERE person_id = ?", (person_id,)
    ).fetchone()
    if row is None:
        _hash(password, _DUMMY_SALT)  # an unknown account takes as long as a known one
        return False
    salt, stored, failed, locked_until = row
    locked = locked_until is not None and datetime.fromisoformat(locked_until) > now
    good = hmac.compare_digest(_hash(password, bytes.fromhex(salt)), stored)
    if locked or not good:
        failed += not locked
        until = (now + LOCK_FOR).isoformat() if failed >= LOCK_AFTER else locked_until
        db.execute(
            "UPDATE accounts SET failed = ?, locked_until = ? WHERE person_id = ?",
            (failed, until, person_id),
        )
        return False
    db.execute(
        "UPDATE accounts SET failed = 0, locked_until = NULL WHERE person_id = ?", (person_id,)
    )
    return True


def start_session(db: sqlite3.Connection, person_id: str, now: datetime) -> str:
    token = secrets.token_urlsafe(32)
    db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now.isoformat(),))
    db.execute(
        "INSERT INTO sessions (token_hash, person_id, expires_at) VALUES (?, ?, ?)",
        (_digest(token), person_id, (now + SESSION_FOR).isoformat()),
    )
    return token


def session_person(db: sqlite3.Connection, token: str, now: datetime) -> str | None:
    if not token:
        return None
    row = db.execute(
        "SELECT person_id FROM sessions WHERE token_hash = ? AND expires_at > ?",
        (_digest(token), now.isoformat()),
    ).fetchone()
    return row[0] if row else None


def end_session(db: sqlite3.Connection, token: str) -> None:
    db.execute("DELETE FROM sessions WHERE token_hash = ?", (_digest(token),))


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT).hex()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
