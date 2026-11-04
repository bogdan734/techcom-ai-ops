"""Model answers kept per input, so a rerun reuses them instead of asking again.

First writer wins, as with note readings: once a question list or a grade exists for an input,
a later run, another model or a changed prompt does not replace it. That is what keeps a
second run identical, and what stops a quiz from changing under someone who is answering it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any


def _key(feature: str, key: str) -> str:
    return f"{feature}|{key}"


def pinned(db: sqlite3.Connection, feature: str, key: str) -> Any | None:
    row = db.execute("SELECT payload FROM pins WHERE key = ?", (_key(feature, key),)).fetchone()
    return json.loads(row[0]) if row else None


def keep(db: sqlite3.Connection, feature: str, key: str, value: Any, model: str, day: date) -> Any:
    """Store ``value`` unless something is already pinned; return what is pinned."""
    db.execute(
        "INSERT OR IGNORE INTO pins (key, kind, payload, model, day) VALUES (?, ?, ?, ?, ?)",
        (_key(feature, key), feature, json.dumps(value, sort_keys=True), model, day.isoformat()),
    )
    return pinned(db, feature, key)
