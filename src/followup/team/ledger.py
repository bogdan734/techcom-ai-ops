"""A run's identity: the inputs it saw and the artefact it produced.

The same inputs must give the same artefact. The run ledger keeps the first artefact digest for
each input digest, so a repeated run is recognised as a replay, and a run that saw the same
inputs and produced something else is flagged as a mismatch instead of passing quietly.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any, Literal

from followup.config import Settings

from .org import Org
from .settings import TeamSettings

# What people and the provider put in, as opposed to what the service derived. The assistant's
# own posts are output, so they are left out.
INPUT_QUERIES = {
    "answers": "SELECT * FROM answers ORDER BY quiz_id, question_id",
    "reviews": "SELECT * FROM reviews ORDER BY quiz_id",
    "chat_messages": (
        "SELECT * FROM chat_messages WHERE author_id != 'assistant' ORDER BY message_id"
    ),
    "call_results": "SELECT * FROM call_results ORDER BY call_key",
}
CONTROL_INPUT = "SELECT * FROM trust_events WHERE kind = 'review_cleared' ORDER BY subject, item"
Replay = Literal["new", "replay", "mismatch"]


def inputs_digest(db: sqlite3.Connection, queries: Mapping[str, str] = INPUT_QUERIES) -> str:
    content = {table: db.execute(query).fetchall() for table, query in queries.items()}
    return _digest(content)


def control_inputs_digest(control: sqlite3.Connection) -> str:
    return inputs_digest(control, {"clearances": CONTROL_INPUT})


def input_digest(
    records: Sequence[Any],
    org: Org,
    settings: Settings,
    team: TeamSettings,
    department_inputs: Mapping[str, str],
) -> str:
    return _digest(
        {
            "records": records,
            "org": org.digest,
            "today": settings.today.isoformat(),
            "policy": asdict(settings.policy),
            "max_failed_runs": settings.max_failed_runs,
            "approve_escalations": settings.approve_escalations,
            "team": team.decision_fields(),
            "departments": dict(department_inputs),
        }
    )


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record_run(
    control: sqlite3.Connection, input_key: str, artefact_digest: str, today: date
) -> Replay:
    """``new`` the first time these inputs are seen; afterwards ``replay`` or ``mismatch``."""
    row = control.execute(
        "SELECT artefact_digest FROM run_ledger WHERE input_digest = ?", (input_key,)
    ).fetchone()
    if row is None:
        control.execute(
            "INSERT INTO run_ledger (input_digest, artefact_digest, run_day) VALUES (?, ?, ?)",
            (input_key, artefact_digest, today.isoformat()),
        )
        return "new"
    return "replay" if row[0] == artefact_digest else "mismatch"


def _digest(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
