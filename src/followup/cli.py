"""Command line entry point: ``followup run [options]`` or ``python -m followup run``."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from dotenv import find_dotenv, load_dotenv

from .config import PROVIDERS, ConfigError, Settings
from .faults import ROUTES, FaultSpecError, parse_faults
from .pipeline import run


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
    run_cmd.add_argument("--out", help="state and artefacts directory (env OUT_DIR, default out)")
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # The .env of the directory the command runs in, not of wherever the package is installed.
    load_dotenv(find_dotenv(usecwd=True))
    overrides = {
        "TASKS_FILE": args.tasks,
        "OUT_DIR": args.out,
        "EVALUATION_DATE": args.today,
        "LLM_PROVIDER": args.provider,
    }
    env = {**os.environ, **{name: value for name, value in overrides.items() if value}}
    try:
        settings = Settings.from_env(env)
        faults = parse_faults(args.fault)
        report = run(settings, faults)
    except (ConfigError, FaultSpecError) as exc:
        print(f"followup: configuration error: {exc}", file=sys.stderr)
        return 1
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
