"""SQLite state: the outbox of follow-ups and the note readings decisions were based on.

The connection runs in autocommit mode, so every state change is on disk before the side
effect it guards. A crash at any point leaves enough behind for the next run to finish
the job without repeating it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from .models import Action, Decision

# Kept in PRAGMA user_version and raised whenever SCHEMA changes. The submitted version never set
# it, so its databases read as 0.
SCHEMA_VERSION = 3

# Statuses of actions that are never posted: they wait for a person.
PARKED_STATUSES: frozenset[str] = frozenset({"dead", "awaiting_approval"})

# attempts counts requests, which vary with retries and reconciliation; failures counts the runs
# that ended without delivering, which is what the dead-letter limit is about. sent_on is the
# evaluation date of the run that confirmed delivery: the reminder cadence counts days from it.
SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    action_id   TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    decision    TEXT NOT NULL,
    target      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    message     TEXT NOT NULL,
    drafted_by  TEXT NOT NULL,
    status      TEXT NOT NULL
                CHECK (status IN ('pending', 'sent', 'failed', 'dead', 'awaiting_approval')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    failures    INTEGER NOT NULL DEFAULT 0,
    message_id  TEXT,
    last_error  TEXT,
    sent_on     TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS actions_task ON actions (task_id);

CREATE TABLE IF NOT EXISTS note_readings (
    cache_key   TEXT PRIMARY KEY,
    answer      TEXT NOT NULL,
    model       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""

_ACTION_COLUMNS = (
    "action_id, task_id, decision, target, reason, message, drafted_by, "
    "status, attempts, failures, message_id, last_error, sent_on"
)

# Every column of the actions table. A rebuild carries over the ones the old table had, and the
# others take their defaults.
_COLUMNS = (
    "action_id",
    "task_id",
    "decision",
    "target",
    "reason",
    "message",
    "drafted_by",
    "status",
    "attempts",
    "failures",
    "message_id",
    "last_error",
    "sent_on",
    "created_at",
    "updated_at",
)

# SQLite cannot alter a CHECK in place, so a table from the submitted version is rebuilt. Its
# index goes first: a rename carries the index along, and CREATE INDEX IF NOT EXISTS would then
# leave the new table without one.
_RENAME_OLD_TABLE = """
DROP INDEX IF EXISTS actions_task;
ALTER TABLE actions RENAME TO actions_old;
"""

# A row sent before sent_on existed has no evaluation date. The day of its last update is the
# closest record, so the cadence can count from it; running this on every rebuild is harmless
# because it only fills gaps.
_BACKFILL_SENT_ON = (
    "UPDATE actions SET sent_on = substr(updated_at, 1, 10) "
    "WHERE status = 'sent' AND sent_on IS NULL;"
)


@dataclass(frozen=True)
class StoredAction:
    action: Action
    status: str
    attempts: int
    failures: int
    message_id: str | None
    last_error: str | None
    sent_on: date | None


class Store:
    def __init__(self, path: Path, today: date | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        # The day a delivery is recorded under: the run's evaluation date, else today in UTC.
        self.today = today or datetime.now(UTC).date()
        self.db = sqlite3.connect(path, isolation_level=None)
        # WAL lets a second run read while another writes; busy_timeout makes it wait, not fail.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        _ensure_schema(self.db)

    @classmethod
    def snapshot(cls, path: Path, today: date | None = None) -> Store:
        """An in-memory copy of the state at ``path``, for a dry run: every write stays in it.

        The file is opened with ``mode=ro``, which SQLite enforces itself, copied and closed.
        A missing file gives an empty store, and neither the file nor its directory is created.
        """
        store = cls.__new__(cls)  # not __init__: it would create the directory and the file
        store.today = today or datetime.now(UTC).date()
        store.db = sqlite3.connect(":memory:", isolation_level=None)
        if path.exists():
            # as_uri escapes '#', '?' and spaces; "file:" + path would open a different file.
            uri = path.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as source:
                source.backup(store.db)
        _ensure_schema(store.db)
        return store

    def close(self) -> None:
        self.db.close()

    # -- outbox ------------------------------------------------------------

    def get_action(self, action_id: str) -> StoredAction | None:
        row = self.db.execute(
            f"SELECT {_ACTION_COLUMNS} FROM actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        return _stored(row) if row else None

    def claim(
        self, action: Action, status: Literal["pending", "awaiting_approval"] = "pending"
    ) -> StoredAction:
        """Record the action under ``status`` unless it is already known; return what is stored.

        The returned row wins over the argument: if another run claimed the same action
        first, its message is the one that must go out under this idempotency key, and its
        status is the one that counts.
        """
        now = _now()
        self.db.execute(
            "INSERT OR IGNORE INTO actions (action_id, task_id, decision, target, reason, "
            "message, drafted_by, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                action.action_id,
                action.task_id,
                action.decision.value,
                action.target,
                action.reason,
                action.message,
                action.drafted_by,
                status,
                now,
                now,
            ),
        )
        stored = self.get_action(action.action_id)
        if stored is None:
            raise RuntimeError(f"action {action.action_id} vanished right after it was claimed")
        return stored

    def start_attempt(self, action_id: str) -> None:
        """Counted before the request leaves, so a crash mid-request is visible next run."""
        self.db.execute(
            "UPDATE actions SET attempts = attempts + 1, updated_at = ? WHERE action_id = ?",
            (_now(), action_id),
        )

    def mark_sent(self, action_id: str, message_id: str | None) -> None:
        self.db.execute(
            "UPDATE actions SET status = 'sent', message_id = ?, last_error = NULL, "
            "sent_on = ?, updated_at = ? WHERE action_id = ?",
            (message_id, self.today.isoformat(), _now(), action_id),
        )

    def mark_failed(self, action_id: str, error: str) -> int:
        """Count one more run in which delivery failed and return how many there are now.

        Only an action still waiting for delivery changes: a sent one may have been delivered
        by a concurrent run, and a dead one is settled.
        """
        self.db.execute(
            "UPDATE actions SET status = 'failed', failures = failures + 1, last_error = ?, "
            "updated_at = ? WHERE action_id = ? AND status IN ('pending', 'failed')",
            (error, _now(), action_id),
        )
        row = self.db.execute(
            "SELECT failures FROM actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        return row[0]

    def mark_dead(self, action_id: str, error: str) -> bool:
        """Park the action: it is never posted again. Guarded like ``mark_failed``.

        False when the row was no longer waiting for delivery, so nothing was parked.
        """
        cursor = self.db.execute(
            "UPDATE actions SET status = 'dead', last_error = ?, updated_at = ? "
            "WHERE action_id = ? AND status IN ('pending', 'failed')",
            (error, _now(), action_id),
        )
        return cursor.rowcount > 0

    def actions_in(self, status: str) -> list[StoredAction]:
        """Oldest first. created_at has a resolution of one second, so the id breaks ties."""
        rows = self.db.execute(
            f"SELECT {_ACTION_COLUMNS} FROM actions WHERE status = ? "
            "ORDER BY created_at, action_id",
            (status,),
        ).fetchall()
        return [_stored(row) for row in rows]

    def requeue(self, action_id: str) -> bool:
        """Release a dead action for the next run; False if it is not dead.

        Attempts are kept on purpose: the request that parked it may have been delivered after
        all, so the next run asks the provider before it resends anything.
        """
        released = self.db.execute(
            "UPDATE actions SET status = 'pending', failures = 0, updated_at = ? "
            "WHERE action_id = ? AND status = 'dead'",
            (_now(), action_id),
        )
        return released.rowcount == 1

    def approve(self, action_id: str) -> bool:
        """Release an escalation held for approval; False if it is not waiting.

        Nothing else changes: it was never posted, so from here the outbox treats it like any
        pending action, and the wording a person reviewed is the wording that goes out.
        """
        released = self.db.execute(
            "UPDATE actions SET status = 'pending', updated_at = ? "
            "WHERE action_id = ? AND status = 'awaiting_approval'",
            (_now(), action_id),
        )
        return released.rowcount == 1

    # -- note readings -----------------------------------------------------

    def get_reading(self, cache_key: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT answer FROM note_readings WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def keep_reading(self, cache_key: str, answer: dict[str, Any], model: str) -> dict[str, Any]:
        """Store a reading unless one exists, and return the one that is kept.

        First writer wins: two overlapping runs that read the same note end up deciding on
        the same answer, so they build the same action and the provider deduplicates it.
        """
        self.db.execute(
            "INSERT OR IGNORE INTO note_readings (cache_key, answer, model, created_at) "
            "VALUES (?, ?, ?, ?)",
            (cache_key, json.dumps(answer, sort_keys=True), model, _now()),
        )
        kept = self.get_reading(cache_key)
        if kept is None:
            raise RuntimeError(f"reading {cache_key} vanished right after it was stored")
        return kept


def _ensure_schema(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    before = after = ""
    if old := {row[1] for row in db.execute("PRAGMA table_info(actions)")}:
        shared = ", ".join(column for column in _COLUMNS if column in old)
        before = _RENAME_OLD_TABLE
        after = (
            f"INSERT INTO actions ({shared}) SELECT {shared} FROM actions_old; "
            f"DROP TABLE actions_old; {_BACKFILL_SENT_ON}"
        )
    # executescript commits an open transaction before it runs, so BEGIN and COMMIT have to be in
    # the script for the setup to be all or nothing; `with db` rolls it back if a statement fails.
    script = f"BEGIN; {before} {SCHEMA} {after} PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;"
    with db:
        db.executescript(script)


def _stored(row: tuple[Any, ...]) -> StoredAction:
    action_id, task_id, decision, target, reason, message, drafted_by = row[:7]
    status, attempts, failures, message_id, last_error, sent_on = row[7:]
    return StoredAction(
        action=Action(
            action_id=action_id,
            task_id=task_id,
            decision=Decision(decision),
            target=target,
            reason=reason,
            message=message,
            drafted_by=drafted_by,
        ),
        status=status,
        attempts=attempts,
        failures=failures,
        message_id=message_id,
        last_error=last_error,
        sent_on=date.fromisoformat(sent_on) if sent_on else None,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
