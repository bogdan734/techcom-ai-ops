"""The snapshot a dry run works on: a copy in memory, so the file is never written."""

from __future__ import annotations

import hashlib
from pathlib import Path

from followup.models import Action, Decision
from followup.store import Store

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
