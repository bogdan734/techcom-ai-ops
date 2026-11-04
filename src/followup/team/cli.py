"""``followup team ...``: the checks, the chat, the levels and the reports from a terminal.

Every command that changes something goes through :mod:`followup.team.actions`, the same
functions the app calls, under the run lock; none of them sends. ``--as`` says who is asking. It
is an assertion and not a login: put a real sign-in in front before using this beyond a team
that trusts each other.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from contextlib import nullcontext

from followup.config import ConfigError, Settings
from followup.run_lock import RunBusy, hold_run_lock

from . import actions
from .cli_db import add_db_parser, run_db
from .context import ActionError, Context, load_context
from .dashboard import build_dashboard, explain, to_markdown
from .vault import VaultError

EXIT_BUSY = 75
_WRITING = frozenset({"answer", "review", "clear", "reply"})


def add_team_parser(commands: argparse._SubParsersAction) -> None:
    team = commands.add_parser("team", help="checks, chat, check levels and reports for a team")
    sub = team.add_subparsers(dest="team_command", required=True)

    def command(name: str, text: str, who: bool = True) -> argparse.ArgumentParser:
        parser = sub.add_parser(name, help=text)
        parser.add_argument("--out", help="state directory (env OUT_DIR)")
        parser.add_argument("--org", help="org file (env ORG_FILE)")
        parser.add_argument("--tasks", help="tasks file (env TASKS_FILE)")
        parser.add_argument("--today", help="evaluation date YYYY-MM-DD (env EVALUATION_DATE)")
        if who:
            parser.add_argument(
                "--as", dest="who", required=True, metavar="PERSON", help="who is asking"
            )
        return parser

    quiz = command("quiz", "show the completion check of a task")
    quiz.add_argument("task", metavar="TASK")
    answer = command("answer", "answer every scored question of a check")
    answer.add_argument("task", metavar="TASK")
    answer.add_argument(
        "pairs", nargs="+", metavar="N ANSWER", help="question number, then the answer"
    )
    review = command("review", "settle a check the grader and the rules split on")
    review.add_argument("task", metavar="TASK")
    verdict = review.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--accept", action="store_true")
    verdict.add_argument("--reject", action="store_true")
    review.add_argument("--note", default="")
    clear = command("clear", "lift a halt after talking to the person")
    clear.add_argument("person", metavar="PERSON")
    trust = command("trust", "check levels in your line, or the record behind one")
    trust.add_argument("--person", metavar="PERSON")
    chat = command("chat", "read or write the thread of a task")
    chat.add_argument("task", metavar="TASK")
    chat.add_argument("text", nargs="?", help="what to write; omit to read the thread")
    reply = command("reply", "write in a thread you are in")
    reply.add_argument("thread", metavar="THREAD")
    reply.add_argument("text")
    support = command("support", "list your tickets, or open one")
    support.add_argument("text", nargs="?", help="what is wrong; omit to list tickets")
    calibrate = command("calibrate", "score the grader against labelled answers", who=False)
    calibrate.add_argument("--gold", default="fixtures/team/gold.json", metavar="FILE")
    report = command("report", "everything one person sees, as text or JSON")
    report.add_argument("--json", action="store_true")
    serve = command("serve", "the app: a local web interface", who=False)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--scenario", metavar="FILE", help="days to step through in the app")
    serve.add_argument("--no-open", action="store_true", help="do not open a browser")
    add_db_parser(sub)


def run_team(args: argparse.Namespace) -> int:
    try:
        settings = _settings(args)
        writes = args.team_command in _WRITING or (
            args.team_command in ("chat", "support") and args.text
        )
        if args.team_command == "db":
            return run_db(args, settings)
        if args.team_command == "serve":
            from .webapp import serve_app

            return serve_app(settings, args)
        ctx = load_context(settings)
        lock = hold_run_lock(settings.out_dir, settings.run_lock) if writes else nullcontext()
        with lock:
            return _HANDLERS[args.team_command](ctx, args)
    except RunBusy as exc:
        print(f"followup: {exc}; try again in a moment", file=sys.stderr)
        return EXIT_BUSY
    except (ActionError, PermissionError, ConfigError, VaultError) as exc:
        print(f"followup: {exc}", file=sys.stderr)
        return 1


def _settings(args: argparse.Namespace) -> Settings:
    overrides = {
        "ORG_FILE": args.org,
        "TASKS_FILE": args.tasks,
        "OUT_DIR": args.out,
        "EVALUATION_DATE": args.today,
    }
    env = {**os.environ, **{name: value for name, value in overrides.items() if value}}
    return Settings.from_env(env)


def _quiz(ctx: Context, args: argparse.Namespace) -> int:
    viewer = ctx.person(args.who)
    task = ctx.task(args.task)
    check = next(
        (
            c
            for c in [*build_dashboard(ctx, viewer).mine, *build_dashboard(ctx, viewer).issued]
            if c.task_id == task.task_id
        ),
        None,
    )
    if check is None or check.status == "none":
        raise ActionError("there is no completion check for you on this task")
    print(f"{check.task_id} {check.title}: check for {check.assignee} ({check.status})")
    for question in check.questions:
        mark = "" if question.graded else "  (not scored)"
        print(f" {question.number}. {question.prompt}{mark}")
        if question.answer is not None:
            print(f"    answered: {question.answer}")
    if check.can_answer:
        print(f"answer with: followup team answer {check.task_id} --as {viewer.id} 1 '...' 2 '...'")
    return 0


def _answer(ctx: Context, args: argparse.Namespace) -> int:
    if len(args.pairs) % 2:
        raise ActionError("give pairs of a question number and an answer")
    try:
        answers = {int(n): text for n, text in zip(args.pairs[0::2], args.pairs[1::2], strict=True)}
    except ValueError as exc:
        raise ActionError("question numbers are whole numbers") from exc
    count = actions.submit_answers(ctx, ctx.person(args.who), args.task, answers)
    print(f"recorded {count} answers; the next run grades them and tells the issuer")
    return 0


def _review(ctx: Context, args: argparse.Namespace) -> int:
    actions.review(ctx, ctx.person(args.who), args.task, args.accept, args.note)
    print(f"review recorded ({'accepted' if args.accept else 'rejected'}); the next run settles it")
    return 0


def _clear(ctx: Context, args: argparse.Namespace) -> int:
    actions.clear(ctx, ctx.person(args.who), args.person)
    print(f"{args.person} is back on the standard plan")
    return 0


def _trust(ctx: Context, args: argparse.Namespace) -> int:
    viewer = ctx.person(args.who)
    if args.person:
        for event in explain(ctx, viewer, args.person):
            print(
                f"{event.day} {event.kind:<15} {'pass' if event.passed else 'fail'}  {event.item}"
            )
        return 0
    board = build_dashboard(ctx, viewer)
    if board.trust is None:
        raise PermissionError("check levels are hidden, or not yours to see")
    for row in board.trust:
        print(f"{row.name:<22} {row.role:<16} {row.level:<10} {row.results} result(s)")
    return 0


def _chat(ctx: Context, args: argparse.Namespace) -> int:
    viewer = ctx.person(args.who)
    if args.text:
        actions.say(ctx, viewer, args.task, args.text)
        print("written; the next run delivers it and the assistant answers from the spec")
        return 0
    task = ctx.task(args.task)
    threads = [t for t in build_dashboard(ctx, viewer).threads if t.task_id == task.task_id]
    for thread in threads:
        for post in thread.posts:
            print(f"{post.day} {post.author}: {post.text}")
    if not threads:
        print("no messages yet")
    return 0


def _reply(ctx: Context, args: argparse.Namespace) -> int:
    actions.reply(ctx, ctx.person(args.who), args.thread, args.text)
    print("written; the next run delivers it")
    return 0


def _support(ctx: Context, args: argparse.Namespace) -> int:
    viewer = ctx.person(args.who)
    if args.text:
        ticket = actions.open_ticket(ctx, viewer, args.text)
        print(f"ticket {ticket} opened; support hears about it on the next run")
        return 0
    for thread in (t for t in build_dashboard(ctx, viewer).threads if t.kind == "support"):
        state = "waiting" if thread.waiting else "answered"
        print(f"{thread.thread_id} {thread.subject} ({state})")
    return 0


def _calibrate(ctx: Context, args: argparse.Namespace) -> int:
    from .calibrate_cli import run_calibration

    return run_calibration(ctx, args.gold)


def _report(ctx: Context, args: argparse.Namespace) -> int:
    board = build_dashboard(ctx, ctx.person(args.who))
    print(
        json.dumps(board.to_dict(), indent=2, ensure_ascii=False)
        if args.json
        else to_markdown(board),
        end="",
    )
    return 0


_HANDLERS: dict[str, Callable[[Context, argparse.Namespace], int]] = {
    "quiz": _quiz,
    "answer": _answer,
    "review": _review,
    "clear": _clear,
    "trust": _trust,
    "chat": _chat,
    "reply": _reply,
    "support": _support,
    "calibrate": _calibrate,
    "report": _report,
}
