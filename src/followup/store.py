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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from .models import Action, Decision

# Kept in PRAGMA user_version and raised whenever SCHEMA changes. The submitted version never set
# it, so its databases read as 0.
SCHEMA_VERSION = 2

# Statuses of actions that are never posted: they wait for a person.
PARKED_STATUSES: frozenset[str] = frozenset({"dead", "awaiting_approval"})

# attempts counts requests, which vary with retries and reconciliation; failures counts the runs
# that ended without delivering, which is what the dead-letter limit is about.
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
    "status, attempts, failures, message_id, last_error"
)

# What a rebuild carries over: the columns the submitted version (schema 0) already had.
_SUBMITTED_COLUMNS = (
    "action_id, task_id, decision, target, reason, message, drafted_by, status, attempts, "
    "message_id, last_error, created_at, updated_at"
)

# SQLite cannot alter a CHECK in place, so a table from the submitted version is rebuilt. Its
# index goes first: a rename carries the index along, and CREATE INDEX IF NOT EXISTS would then
# leave the new table without one.
_RENAME_OLD_TABLE = """
DROP INDEX IF EXISTS actions_task;
ALTER TABLE actions RENAME TO actions_old;
"""
_COPY_OLD_ROWS = f"""
INSERT INTO actions ({_SUBMITTED_COLUMNS}) SELECT {_SUBMITTED_COLUMNS} FROM actions_old;
DROP TABLE actions_old;
"""


@dataclass(frozen=True)
class StoredAction:
    action: Action
    status: str
    attempts: int
    failures: int
    message_id: str | None
    last_error: str | None


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        # WAL lets a second run read while another writes; busy_timeout makes it wait, not fail.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        _ensure_schema(self.db)

    @classmethod
    def snapshot(cls, path: Path) -> Store:
        """An in-memory copy of the state at ``path``, for a dry run: every write stays in it.

        The file is opened with ``mode=ro``, which SQLite enforces itself, copied and closed.
        A missing file gives an empty store, and neither the file nor its directory is created.
        """
        store = cls.__new__(cls)  # not __init__: it would create the directory and the file
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
            "updated_at = ? WHERE action_id = ?",
            (message_id, _now(), action_id),
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

    def mark_dead(self, action_id: str, error: str) -> None:
        """Park the action: it is never posted again. Guarded like ``mark_failed``."""
        self.db.execute(
            "UPDATE actions SET status = 'dead', last_error = ?, updated_at = ? "
            "WHERE action_id = ? AND status IN ('pending', 'failed')",
            (error, _now(), action_id),
        )

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
    has_actions = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'actions'"
    ).fetchone()
    before, after = (_RENAME_OLD_TABLE, _COPY_OLD_ROWS) if has_actions else ("", "")
    # executescript commits an open transaction before it runs, so BEGIN and COMMIT have to be in
    # the script for the setup to be all or nothing; `with db` rolls it back if a statement fails.
    script = f"BEGIN; {before} {SCHEMA} {after} PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;"
    with db:
        db.executescript(script)


def _stored(row: tuple[Any, ...]) -> StoredAction:
    action_id, task_id, decision, target, reason, message, drafted_by = row[:7]
    status, attempts, failures, message_id, last_error = row[7:]
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
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
