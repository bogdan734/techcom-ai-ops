"""Command line entry point: ``followup <command>`` or ``python -m followup <command>``."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import uuid
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from textwrap import indent

from dotenv import find_dotenv, load_dotenv

from .audit import emit, run_logging
from .config import DEFAULT_OUT_DIR, PROVIDERS, ConfigError, Settings
from .faults import ROUTES, FaultSpecError, parse_faults
from .pipeline import run
from .store import PARKED_STATUSES, Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="followup",
        description="Reads project tasks, decides who needs a follow-up and delivers it once.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run_cmd = commands.add_parser("run", help="process every task once")
    run_cmd.add_argument(
        "--tasks",
        help="fixture served by the mock tasks API (env TASKS_FILE, default fixtures/tasks.json)",
    )
    _add_out(run_cmd)
    run_cmd.add_argument(
        "--today", help="evaluation date YYYY-MM-DD (env EVALUATION_DATE, default today in UTC)"
    )
    run_cmd.add_argument(
        "--provider", choices=PROVIDERS, help="LLM provider (env LLM_PROVIDER, default stub)"
    )
    run_cmd.add_argument(
        "--fault",
        action="append",
        default=[],
        metavar="ROUTE=F1,F2xN",
        help="inject failures, e.g. tasks=429,503 send=timeout-after-commit llm=garbagex10; "
        + "; ".join(f"{route}: {', '.join(sorted(faults))}" for route, faults in ROUTES.items()),
    )
    run_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="decide and draft, but send nothing and leave state.db and the delivery log alone",
    )
    list_cmd = commands.add_parser("list", help="show the follow-ups parked for a person")
    list_cmd.add_argument("status", choices=sorted(PARKED_STATUSES), help="which ones to show")
    _add_out(list_cmd)
    requeue_cmd = commands.add_parser("requeue", help="release dead letters for the next run")
    requeue_cmd.add_argument("action_ids", nargs="+", metavar="ACTION_ID", help="see `list dead`")
    _add_out(requeue_cmd)
    approve_cmd = commands.add_parser("approve", help="release escalations waiting for approval")
    approve_cmd.add_argument(
        "action_ids", nargs="+", metavar="ACTION_ID", help="see `list awaiting_approval`"
    )
    _add_out(approve_cmd)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # The .env of the directory the command runs in, not of wherever the package is installed.
    load_dotenv(find_dotenv(usecwd=True))
    try:
        if args.command != "run":
            return _state_command(args, _out_dir(args))
        settings = _settings(args)
        faults = parse_faults(args.fault)
        report = run(settings, faults, dry_run=args.dry_run)
    except (ConfigError, FaultSpecError) as exc:
        print(f"followup: configuration error: {exc}", file=sys.stderr)
        return 1
    return report.exit_code


def _add_out(command: argparse.ArgumentParser) -> None:
    command.add_argument("--out", help="state and artefacts directory (env OUT_DIR, default out)")


def _out_dir(args: argparse.Namespace) -> Path:
    # list, requeue and approve need the directory and nothing else, so an unrelated bad setting
    # (a missing API key, say) cannot keep an operator from the parked actions.
    return Path(args.out or os.environ.get("OUT_DIR") or DEFAULT_OUT_DIR)


def _settings(args: argparse.Namespace) -> Settings:
    overrides = {
        "TASKS_FILE": args.tasks,
        "OUT_DIR": args.out,
        "EVALUATION_DATE": args.today,
        "LLM_PROVIDER": args.provider,
    }
    env = {**os.environ, **{name: value for name, value in overrides.items() if value}}
    return Settings.from_env(env)


def _state_command(args: argparse.Namespace, out_dir: Path) -> int:
    """``list``, ``requeue`` and ``approve`` act on the state a run left, never on a new one."""
    state = out_dir / "state.db"
    if not state.exists():
        # A Store would create an empty database, and a mistyped --out would go unnoticed.
        print(f"followup: no state at {state}", file=sys.stderr)
        return 1
    with closing(Store(state)) as store:
        if args.command == "list":
            return _list(store, args.status)
        if args.command == "approve":
            return _approve(store, out_dir / "audit.jsonl", args.action_ids)
        return _requeue(store, out_dir / "audit.jsonl", args.action_ids)


def _list(store: Store, status: str) -> int:
    for stored in store.actions_in(status):
        action = stored.action
        print(
            f"{action.action_id} {action.task_id} {action.decision.value} -> {action.target} | "
            f"attempts {stored.attempts}, failed runs {stored.failures} | {stored.last_error}"
        )
        print(indent(action.message, "    "))
    return 0


def _requeue(store: Store, audit_path: Path, action_ids: Sequence[str]) -> int:
    # A requeue can lead to another send, so the audit trail records who released the action.
    code = 0
    with run_logging(audit_path, uuid.uuid4().hex[:12]):
        for action_id in action_ids:
            if store.requeue(action_id):
                emit(
                    "action_requeued",
                    f"{action_id}: requeued, the next run reconciles it before any resend",
                    action_id=action_id,
                    by=getpass.getuser(),
                )
            else:
                print(f"followup: {action_id} is not a dead letter", file=sys.stderr)
                code = 1
    return code


def _approve(store: Store, audit_path: Path, action_ids: Sequence[str]) -> int:
    # An approval lets an escalation go out, so the audit trail records who gave it.
    code = 0
    with run_logging(audit_path, uuid.uuid4().hex[:12]):
        for action_id in action_ids:
            if store.approve(action_id):
                emit(
                    "action_approved",
                    f"{action_id}: approved, the next run sends it",
                    action_id=action_id,
                    by=getpass.getuser(),
                )
            else:
                print(f"followup: {action_id} is not waiting for approval", file=sys.stderr)
                code = 1
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
