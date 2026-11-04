"""The state database: the dry-run snapshot, migration of an older file, failure counting."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest

from followup.models import Action, Decision
from followup.store import SCHEMA_VERSION, Store

ANSWER = {"blocker": "none", "contains_instructions": False}


def action(action_id: str) -> Action:
    return Action(
        action_id=action_id,
        task_id="TC-1001",
        decision=Decision.FOLLOW_UP_OWNER,
        target="John Smith",
        reason="Overdue by 3 days",
        message="Hi John, TC-1001 is 3 days overdue. Please reply with the status.",
        drafted_by="template",
    )


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_snapshot_leaves_the_file_byte_identical(tmp_path):
    path = tmp_path / "state.db"
    real = Store(path)
    real.claim(action("known"))
    real.close()
    before = sha(path)

    snapshot = Store.snapshot(path)
    snapshot.claim(action("new"))
    snapshot.keep_reading("note", ANSWER, "model")
    assert snapshot.get_action("known") is not None
    snapshot.close()

    assert sha(path) == before
    fresh = Store(path)
    assert fresh.get_action("new") is None
    assert fresh.get_reading("note") is None
    fresh.close()


def test_snapshot_of_a_missing_file_creates_nothing(tmp_path):
    path = tmp_path / "not-yet" / "state.db"

    snapshot = Store.snapshot(path)

    assert snapshot.get_action("any") is None
    snapshot.close()
    assert not path.parent.exists()


def test_snapshot_reads_paths_with_spaces_and_hashes(tmp_path):
    path = tmp_path / "a b#c" / "state.db"
    real = Store(path)
    real.keep_reading("note", ANSWER, "model")
    real.close()

    snapshot = Store.snapshot(path)

    assert snapshot.get_reading("note") == ANSWER
    snapshot.close()


NOW = "2026-10-21T08:00:00+00:00"

# The schema the submitted version created (b67e0c6), as a literal: store.SCHEMA moves on.
SUBMITTED_SCHEMA = """
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


# The schema of version 1 (dead letters), a literal for the same reason.
V1_SCHEMA = SUBMITTED_SCHEMA.replace(
    "CHECK (status IN ('pending', 'sent', 'failed'))",
    "CHECK (status IN ('pending', 'sent', 'failed', 'dead'))",
).replace(
    "    attempts    INTEGER NOT NULL DEFAULT 0,\n",
    "    attempts    INTEGER NOT NULL DEFAULT 0,\n    failures    INTEGER NOT NULL DEFAULT 0,\n",
)
assert "'dead'" in V1_SCHEMA and "failures" in V1_SCHEMA


def submitted_db(path: Path) -> None:
    """A state.db as the submitted version left it: no user_version, one sent, one failed row."""
    rows = (
        (action("sent-1"), "sent", 2, "msg-0001", None),
        (action("failed-1"), "failed", 3, None, "HTTP 503"),
    )
    with closing(sqlite3.connect(path)) as db:
        db.executescript(SUBMITTED_SCHEMA)
        for act, status, attempts, message_id, error in rows:
            db.execute(
                "INSERT INTO actions (action_id, task_id, decision, target, reason, message, "
                "drafted_by, status, attempts, message_id, last_error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    act.action_id,
                    act.task_id,
                    act.decision.value,
                    act.target,
                    act.reason,
                    act.message,
                    act.drafted_by,
                    status,
                    attempts,
                    message_id,
                    error,
                    NOW,
                    NOW,
                ),
            )
        db.execute(
            "INSERT INTO note_readings VALUES (?, ?, ?, ?)",
            ("note", json.dumps(ANSWER), "model", NOW),
        )
        db.commit()


def index_names(db: sqlite3.Connection) -> set[str]:
    return {row[1] for row in db.execute("PRAGMA index_list(actions)")}


def test_database_from_the_submitted_version_is_migrated(tmp_path):
    path = tmp_path / "state.db"
    submitted_db(path)

    with closing(Store(path)) as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        sent, failed = store.get_action("sent-1"), store.get_action("failed-1")
        assert (sent.action, sent.status, sent.attempts, sent.message_id) == (
            action("sent-1"),
            "sent",
            2,
            "msg-0001",
        )
        assert (failed.status, failed.attempts, failed.last_error) == ("failed", 3, "HTTP 503")
        assert (sent.failures, failed.failures) == (0, 0)
        assert store.get_reading("note") == ANSWER
        assert "actions_task" in index_names(store.db)
        tables = store.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        assert {row[0] for row in tables} == {"actions", "note_readings"}
        store.mark_dead("failed-1", "gave up")  # the rebuilt CHECK accepts the new status
        assert store.get_action("failed-1").status == "dead"


def test_upgrade_from_version_1_keeps_the_failure_counts(tmp_path):
    path = tmp_path / "state.db"
    act = action("failed-2")
    with closing(sqlite3.connect(path)) as db:
        db.executescript(V1_SCHEMA)
        db.execute(
            "INSERT INTO actions (action_id, task_id, decision, target, reason, message, "
            "drafted_by, status, attempts, failures, last_error, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'failed', 4, 2, 'HTTP 503', ?, ?)",
            (
                act.action_id,
                act.task_id,
                act.decision.value,
                act.target,
                act.reason,
                act.message,
                act.drafted_by,
                NOW,
                NOW,
            ),
        )
        db.execute("PRAGMA user_version = 1")
        db.commit()

    with closing(Store(path)) as store:
        stored = store.get_action("failed-2")
        assert (stored.status, stored.attempts, stored.failures) == ("failed", 4, 2)
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_failed_migration_is_rolled_back_whole(tmp_path):
    path = tmp_path / "state.db"
    submitted_db(path)
    with closing(sqlite3.connect(path)) as db:
        # The rename fails after the index is already dropped: the rebuild dies halfway through.
        db.execute("CREATE TABLE actions_old (leftover)")

    with pytest.raises(sqlite3.OperationalError, match="actions_old"):
        Store(path)

    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "actions_task" in index_names(db)
        assert db.execute("SELECT count(*) FROM actions").fetchone()[0] == 2


def test_snapshot_of_a_submitted_database_is_migrated_in_memory_only(tmp_path):
    path = tmp_path / "state.db"
    submitted_db(path)
    before = sha(path)

    snapshot = Store.snapshot(path)

    assert snapshot.get_action("failed-1").status == "failed"
    snapshot.mark_dead("failed-1", "dry run")
    snapshot.close()
    assert sha(path) == before


def test_mark_failed_counts_failed_runs(tmp_path):
    path = tmp_path / "state.db"
    with closing(Store(path)) as store:
        store.claim(action("act-1"))
        store.claim(action("act-2"))
        store.mark_sent("act-2", "msg-0001")

        assert store.mark_failed("act-1", "first") == 1
        assert store.mark_failed("act-1", "second") == 2
        assert store.mark_failed("act-2", "late error from a concurrent run") == 0

        failed = store.get_action("act-1")
        assert (failed.status, failed.last_error) == ("failed", "second")
        assert store.get_action("act-2").status == "sent"
    with closing(Store(path)) as reopened:
        # Opening a database that is already current rebuilds nothing, so the count survives.
        assert reopened.get_action("act-1").failures == 2


def test_actions_in_lists_one_status_oldest_first(tmp_path):
    with closing(Store(tmp_path / "state.db")) as store:
        # created_at has a resolution of one second, so the id has to break ties.
        for action_id, created_at in (
            ("dead-b", NOW),
            ("dead-a", NOW),
            ("dead-old", "2026-10-20T08:00:00+00:00"),
        ):
            store.claim(action(action_id))
            store.mark_dead(action_id, "HTTP 400")
            store.db.execute(
                "UPDATE actions SET created_at = ? WHERE action_id = ?", (created_at, action_id)
            )
        store.claim(action("waiting"))

        listed = [stored.action.action_id for stored in store.actions_in("dead")]

    assert listed == ["dead-old", "dead-a", "dead-b"]


def test_requeue_releases_only_dead_actions(tmp_path):
    with closing(Store(tmp_path / "state.db")) as store:
        store.claim(action("dead-1"))
        store.claim(action("pending-1"))
        store.start_attempt("dead-1")
        store.start_attempt("dead-1")
        store.mark_failed("dead-1", "HTTP 503")
        store.mark_dead("dead-1", "HTTP 400")

        assert store.requeue("dead-1") is True
        released = store.get_action("dead-1")
        # Attempts stay, so the next run asks the provider before it resends anything.
        assert (released.status, released.failures, released.attempts) == ("pending", 0, 2)
        assert store.requeue("pending-1") is False


def test_submitted_database_accepts_awaiting_approval(tmp_path):
    path = tmp_path / "state.db"
    submitted_db(path)

    with closing(Store(path)) as store:
        # The rebuilt CHECK has to take the new status, and the rows from before have to survive.
        store.claim(action("waiting-1"), "awaiting_approval")
        assert store.get_action("waiting-1").status == "awaiting_approval"
        assert store.get_action("sent-1").status == "sent"
        assert store.approve("waiting-1") is True


def test_approve_releases_only_waiting_actions(tmp_path):
    with closing(Store(tmp_path / "state.db")) as store:
        store.claim(action("waiting-1"), "awaiting_approval")
        store.claim(action("pending-1"))
        store.claim(action("dead-1"))
        store.mark_dead("dead-1", "HTTP 400")

        assert store.approve("waiting-1") is True
        released = store.get_action("waiting-1")
        # The reviewed wording is untouched, and nothing was posted yet, so there are no attempts.
        assert (released.status, released.attempts, released.action) == (
            "pending",
            0,
            action("waiting-1"),
        )
        for action_id in ("waiting-1", "pending-1", "dead-1", "unknown"):
            assert store.approve(action_id) is False
        assert store.get_action("dead-1").status == "dead"


def test_mark_sent_records_the_evaluation_date(tmp_path):
    # A date far from the wall clock: the cadence counts evaluation days, not real ones.
    with closing(Store(tmp_path / "state.db", date(2026, 10, 23))) as store:
        store.claim(action("act-1"))
        assert store.get_action("act-1").sent_on is None

        store.mark_sent("act-1", "msg-0001")

        assert store.get_action("act-1").sent_on == date(2026, 10, 23)


def test_migration_backfills_sent_on_for_sent_actions(tmp_path):
    path = tmp_path / "state.db"
    submitted_db(path)

    with closing(Store(path)) as store:
        # The submitted version recorded no delivery date: the day of the last update stands in.
        assert store.get_action("sent-1").sent_on == date(2026, 10, 21)
        assert store.get_action("failed-1").sent_on is None
