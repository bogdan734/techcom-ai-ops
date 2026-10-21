"""SQLite state: the outbox of follow-ups and the note readings decisions were based on.

The connection runs in autocommit mode, so every state change is on disk before the side
effect it guards. A crash at any point leaves enough behind for the next run to finish
the job without repeating it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Action, Decision

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    action_id   TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    decision    TEXT NOT NULL,
    target      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    message     TEXT NOT NULL,
    drafted_by  TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
    attempts    INTEGER NOT NULL DEFAULT 0,
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
    "status, attempts, message_id, last_error"
)


@dataclass(frozen=True)
class StoredAction:
    action: Action
    status: str
    attempts: int
    message_id: str | None
    last_error: str | None


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        # WAL lets a second run read while another writes; busy_timeout makes it wait, not fail.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- outbox ------------------------------------------------------------

    def get_action(self, action_id: str) -> StoredAction | None:
        row = self.db.execute(
            f"SELECT {_ACTION_COLUMNS} FROM actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        return _stored(row) if row else None

    def claim(self, action: Action) -> StoredAction:
        """Record the action as pending unless it is already known; return what is stored.

        The returned row wins over the argument: if another run claimed the same action
        first, its message is the one that must go out under this idempotency key.
        """
        now = _now()
        self.db.execute(
            "INSERT OR IGNORE INTO actions (action_id, task_id, decision, target, reason, "
            "message, drafted_by, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                action.action_id,
                action.task_id,
                action.decision.value,
                action.target,
                action.reason,
                action.message,
                action.drafted_by,
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

    def mark_failed(self, action_id: str, error: str) -> None:
        """Never downgrades a sent action, which a concurrent run may have delivered."""
        self.db.execute(
            "UPDATE actions SET status = 'failed', last_error = ?, updated_at = ? "
            "WHERE action_id = ? AND status != 'sent'",
            (error, _now(), action_id),
        )

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


def _stored(row: tuple[Any, ...]) -> StoredAction:
    action_id, task_id, decision, target, reason, message, drafted_by = row[:7]
    status, attempts, message_id, last_error = row[7:]
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
        message_id=message_id,
        last_error=last_error,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
