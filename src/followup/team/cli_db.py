"""``followup team db``: look at the department files, check them, migrate them, split the old one.

``status`` and ``verify`` never create a file. ``migrate`` and ``split`` change data, so they
take the run lock and say where the backup went.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing

from followup.config import Settings
from followup.run_lock import hold_run_lock

from .context import load_context
from .split import check_legacy, split
from .vault import Vault, VaultError

_COUNTED = ("actions", "task_history", "quizzes", "answers", "threads", "chat_messages")


def add_db_parser(team_commands: argparse._SubParsersAction) -> None:
    db = team_commands.add_parser(
        "db", help="department database files: status, verify, migrate, split"
    )
    kinds = db.add_subparsers(dest="db_command", required=True)
    for name, text in (
        ("status", "list the files, their steps and row counts"),
        ("verify", "check every file; exit 1 on any problem"),
        ("migrate", "apply pending steps, each after a backup"),
        ("split", "move the old single state.db into department files"),
    ):
        _common(kinds.add_parser(name, help=text))


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", help="state directory (env OUT_DIR)")
    parser.add_argument("--org", help="org file (env ORG_FILE)")
    parser.add_argument("--tasks", help="tasks file (env TASKS_FILE); split needs it")
    parser.add_argument("--today", help="evaluation date YYYY-MM-DD (env EVALUATION_DATE)")


def run_db(args: argparse.Namespace, settings: Settings) -> int:
    team = settings.team
    assert team is not None
    vault = Vault(settings.out_dir / "state", team.org, team.trust_visibility)
    if args.db_command == "status":
        return _status(vault)
    if args.db_command == "verify":
        return _verify(vault, settings)
    with hold_run_lock(settings.out_dir, settings.run_lock):
        if args.db_command == "migrate":
            return _migrate(vault)
        return _split(settings)


def _status(vault: Vault) -> int:
    print(f"{vault.root}")
    for name in vault.known():
        if not vault.path(name).exists():
            print(f"  {name:<14} missing")
            continue
        try:
            status = vault.check(name)
        except VaultError as exc:
            print(f"  {name:<14} PROBLEM {exc}")
            continue
        pending = f", pending {', '.join(status.pending)}" if status.pending else ""
        steps = f"steps {len(status.applied)}, base schema {status.base_version}{pending}"
        print(f"  {name:<14} {steps}{_counts(vault, name)}")
    return 0


def _counts(vault: Vault, name: str) -> str:
    db = vault.open(name)
    try:
        have = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        parts = [
            f"{table} {db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]}"
            for table in _COUNTED
            if table in have
        ]
    except sqlite3.DatabaseError:
        return ""  # a viewer-less service connection reads everything; anything else is a problem
    finally:
        db.close()
    return "; " + ", ".join(parts) if parts else ""


def _verify(vault: Vault, settings: Settings) -> int:
    problems = 0
    try:
        check_legacy(settings.out_dir, vault)
    except VaultError as exc:
        print(f"PROBLEM {exc}", file=sys.stderr)
        problems += 1
    for name in vault.names():
        try:
            status = vault.check(name)
            with closing(
                sqlite3.connect(f"{vault.path(name).resolve().as_uri()}?mode=ro", uri=True)
            ) as db:
                integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok" or status.pending:
                raise VaultError(f"{name}: integrity {integrity}, pending {list(status.pending)}")
            print(f"ok   {name}")
        except VaultError as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            problems += 1
    return 1 if problems else 0


def _migrate(vault: Vault) -> int:
    done = False
    for name in vault.names():
        for step in vault.migrate(name):
            done = True
            backup = vault.path(name).with_name(f"{name}.before-{step}.db")
            print(f"{name}: applied {step}; backup {backup}")
    if not done:
        print("nothing to migrate")
    return 0


def _split(settings: Settings) -> int:
    ctx = load_context(settings)
    report = split(
        settings.out_dir, ctx.records, ctx.org, ctx.team.trust_visibility, settings.today
    )
    print(f"split state.db ({report.source_digest[:12]}) into {settings.out_dir / 'state'}")
    for table, counts in (("actions", report.actions), ("readings", report.readings)):
        print(f"  {table}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    print("state.db was only read; keep it as the record of the old version")
    return 0
