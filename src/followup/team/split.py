"""One-way move of the old single ``state.db`` into department files, losing no sent record.

The old file holds the memory that stops a follow-up from going out twice. Starting department
files empty would send everything again, so the split copies every row to the department of its
task, builds the result beside the final name and renames it in one step. ``state.db`` is only
read. Afterwards it must not change: a marker holds a digest of its contents, and a run that
finds the file different refuses to start (an older version is still writing there).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from followup.normalize import normalise_all
from followup.notes import reading_key
from followup.store import Store

from .org import UNROUTED, Org
from .routes import pin_routes
from .schema import CONTROL
from .vault import Vault, VaultError

MARKER = "split_from"
_TABLES = (("actions", "action_id"), ("note_readings", "cache_key"))


@dataclass(frozen=True)
class SplitReport:
    source_digest: str
    actions: Mapping[str, int]
    readings: Mapping[str, int]


def logical_digest(db: sqlite3.Connection) -> str:
    """A digest of what ``state.db`` holds, independent of its journal and page layout."""
    content = {
        table: db.execute(f"SELECT * FROM {table} ORDER BY {key}").fetchall()
        for table, key in _TABLES
    }
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode("utf-8")).hexdigest()


def split(
    out_dir: Path, records: Sequence[Any], org: Org, visibility: str, today: date
) -> SplitReport:
    source, final = out_dir / "state.db", out_dir / "state"
    if not source.exists():
        raise VaultError(f"{source} does not exist: nothing to split")
    if final.exists():
        raise VaultError(f"{final} already exists: the split was done")
    tasks, _ = normalise_all(records)
    building = out_dir / "state.split-tmp"
    shutil.rmtree(building, ignore_errors=True)
    snapshot = Store.snapshot(source)
    try:
        digest = logical_digest(snapshot.db)
        vault = Vault(building, org, visibility)
        for name in vault.known():
            vault.create(name)
        control = vault.open(CONTROL)
        routes = pin_routes(control, tasks, org, today)
        reading_routes = {reading_key(task): routes[task.task_id] for task in tasks}
        report = _copy(snapshot, vault, routes, reading_routes, digest)
        control.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (MARKER, digest))
        control.close()
        os.replace(building, final)
        return report
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise
    finally:
        snapshot.close()


def check_legacy(out_dir: Path, vault: Vault) -> None:
    """Refuse to run beside a ``state.db`` that holds actions the department files do not know."""
    source = out_dir / "state.db"
    if not source.exists():
        return
    snapshot = Store.snapshot(source)
    try:
        count = snapshot.db.execute("SELECT count(*) FROM actions").fetchone()[0]
        if count == 0:
            return
        marker = vault.meta(CONTROL, MARKER) if vault.path(CONTROL).exists() else None
        if marker is None:
            raise VaultError(
                f"{source} holds {count} actions and was never split: "
                "run `followup team db split` first, or every follow-up would go out again"
            )
        if marker != logical_digest(snapshot.db):
            raise VaultError(
                f"{source} changed after the split: an older run is still writing there"
            )
    finally:
        snapshot.close()


def _copy(
    snapshot: Store,
    vault: Vault,
    routes: Mapping[str, str],
    reading_routes: Mapping[str, str],
    digest: str,
) -> SplitReport:
    counts: dict[str, dict[str, int]] = {"actions": {}, "note_readings": {}}
    stores: dict[str, Store] = {}
    try:
        for table, key in _TABLES:
            rows = snapshot.db.execute(f"SELECT * FROM {table} ORDER BY {key}")
            names = [column[0] for column in rows.description]
            for row in rows.fetchall():
                record = dict(zip(names, row, strict=True))
                if table == "actions":
                    department = routes.get(record["task_id"], UNROUTED)
                else:
                    department = reading_routes.get(record["cache_key"], UNROUTED)
                target = stores.get(department) or stores.setdefault(
                    department, Store(vault.path(department))
                )
                columns = [c for c in names if c in _columns(target.db, table)]
                target.db.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    [record[c] for c in columns],
                )
                counts[table][department] = counts[table].get(department, 0) + 1
            expected = snapshot.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if sum(counts[table].values()) != expected:
                raise VaultError(
                    f"split lost rows of {table}: {sum(counts[table].values())}/{expected}"
                )
    finally:
        for store in stores.values():
            store.close()
    return SplitReport(digest, counts["actions"], counts["note_readings"])


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
