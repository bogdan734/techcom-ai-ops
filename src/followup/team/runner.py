"""The team pass of one run, in three phases so a rerun cannot change what was decided.

absorb   writes what is new: history, quizzes, grades, trust events and the assistant's replies;
decide   builds every message from stored state, the inputs and today (no clock, no randomness);
deliver  hands each message to the same outbox the base follow-ups use.

The base rules still decide the follow-ups; they run per task against the store of the task's
department. ``state.db`` is never opened here.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2

from followup.api import ApiError, MessagesApi
from followup.config import Settings
from followup.delivery import Outbox
from followup.drafting import Drafter
from followup.llm import LLMGateway
from followup.models import Task, TaskOutcome
from followup.notes import NoteReader
from followup.pipeline import FollowUpService
from followup.retry import CircuitBreaker, RetryPolicy
from followup.store import Store

from .assistant import Assistant
from .calls import AnsweredBy, CallsApi, pending_calls, record_result, settle_result
from .chat import ASSISTANT, chat_notices, pending_questions, post
from .check import ModelAudit
from .completion import CheckResult, CompletionCheck
from .extras import TaskExtras, load_extras
from .grading import Grader
from .ledger import (
    Replay,
    control_inputs_digest,
    file_digest,
    input_digest,
    inputs_digest,
    record_run,
)
from .notices import (
    Channel,
    Kind,
    Message,
    begin_activation,
    history_for,
    lifecycle_notices,
    record_history,
    to_action,
)
from .org import Role
from .quiz import QuizMaker, open_quiz
from .routes import pin_routes
from .schema import CONTROL
from .settings import TeamSettings
from .split import check_legacy
from .trust import Ledger
from .vault import Vault

ARTEFACT = "team.json"
_UNSETTLED = {"failed", "deferred", "dead"}


@dataclass
class TeamResult:
    outcomes: list[TaskOutcome]
    undelivered: int
    replay: Replay
    artefact_digest: str
    messages: int = 0


@dataclass
class _Department:
    name: str
    store: Store
    db: sqlite3.Connection
    outbox: Outbox
    calls: Outbox
    check: CompletionCheck
    assistant: Assistant
    messages: list[Message] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    waiting: list[dict[str, str]] = field(default_factory=list)


def team_run(
    settings: Settings,
    team: TeamSettings,
    records: Sequence[Any],
    tasks: Sequence[Task],
    http: httpx2.Client,
    policy: RetryPolicy,
    gateway: LLMGateway,
    audit_gateway: LLMGateway,
    calls_http: httpx2.Client | None = None,
) -> TeamResult:
    """``calls_http``: a real voice provider's client; without one, calls use the mock."""
    out, today, org = settings.out_dir, settings.today, team.org
    vault = Vault(out / "state", org, team.trust_visibility)
    check_legacy(out, vault)
    for name in vault.known():
        vault.ensure(name)
    control = vault.open(CONTROL)
    departments: dict[str, _Department] = {}
    try:
        names = [name for name in vault.known() if name != CONTROL]
        input_key = input_digest(
            records,
            org,
            settings,
            team,
            {
                **{n: _inputs(vault, n) for n in names},
                CONTROL: control_inputs_digest(control),
            },
        )
        routes = pin_routes(control, tasks, org, today)
        extras = load_extras(records, org)
        audit = ModelAudit(Ledger(control, "model_events"), gateway.model, team, today)
        trust = Ledger(control, "trust_events")
        breaker = CircuitBreaker()
        calls_api = CallsApi(calls_http or http, policy)
        for name in names:
            departments[name] = _open(
                name,
                vault,
                settings,
                team,
                http,
                policy,
                gateway,
                audit_gateway,
                audit,
                trust,
                breaker,
                calls_api,
            )

        # The base rules, per task, against the store of the task's department.
        people = {task.owner for task in tasks if task.owner}
        outcomes: list[TaskOutcome] = []
        for name, department in departments.items():
            service = FollowUpService(
                today,
                settings.policy,
                department.store,
                NoteReader(gateway, department.store),
                Drafter(gateway, today),
                department.outbox,
                settings.approve_escalations,
            )
            mine = sorted((t for t in tasks if routes[t.task_id] == name), key=lambda t: t.task_id)
            outcomes += [service.handle(task, people) for task in mine]
        outcomes.sort(key=lambda outcome: outcome.task_id)

        # absorb, then decide
        open_by_owner: dict[str, list[Task]] = {}
        for task in tasks:
            if task.owner:
                open_by_owner.setdefault(task.owner, []).append(task)
        for name, department in departments.items():
            mine = sorted((t for t in tasks if routes[t.task_id] == name), key=lambda t: t.task_id)
            _settle_calls(department, calls_api)
            _team_pass(department, mine, extras, open_by_owner, team, today)
        boss = org.with_role(Role.BOSS)[0]
        departments[boss.department].messages += next(
            iter(departments.values())
        ).check.model_notices()

        undelivered = _deliver(departments, org, calls_api, team, today)
        path = out / ARTEFACT
        _write_artefact(path, settings, departments)
        digest = file_digest(path)
        replay = record_run(control, input_key, digest, today)
        count = sum(len(d.messages) for d in departments.values())
        return TeamResult(outcomes, undelivered, replay, digest, count)
    finally:
        for department in departments.values():
            department.db.close()
            department.store.close()
        control.close()


def _inputs(vault: Vault, name: str) -> str:
    db = vault.open(name)
    try:
        return inputs_digest(db)
    finally:
        db.close()


def _open(
    name: str,
    vault: Vault,
    settings: Settings,
    team: TeamSettings,
    http: httpx2.Client,
    policy: RetryPolicy,
    gateway: LLMGateway,
    audit_gateway: LLMGateway,
    audit: ModelAudit,
    trust: Ledger,
    breaker: CircuitBreaker,
    calls_api: CallsApi,
) -> _Department:
    store = Store(vault.path(name), settings.today)
    db = vault.open(name)
    outbox = Outbox(store, MessagesApi(http, policy), policy, breaker, settings.max_failed_runs)
    calls = Outbox(store, calls_api, policy, breaker, settings.max_failed_runs)
    check = CompletionCheck(
        team.org,
        team,
        trust,
        QuizMaker(gateway, db, audit),
        Grader(gateway, audit_gateway, db, audit, team.quiz_pass_score),
        gateway,
        audit,
        team.code_root,
    )
    return _Department(name, store, db, outbox, calls, check, Assistant(gateway, db, audit))


def _team_pass(
    department: _Department,
    tasks: Sequence[Task],
    extras: Mapping[str, TaskExtras],
    open_by_owner: Mapping[str, Sequence[Task]],
    team: TeamSettings,
    today,
) -> None:
    db = department.db
    activation = begin_activation(db, today)
    for task in tasks:
        extra = extras.get(task.task_id)
        record_history(db, task, extra, today, activation)
        rows = history_for(db, task.task_id)
        department.messages += lifecycle_notices(
            task, extra, rows, open_by_owner, team.org, today, team.new_task_window_days
        )
        result = department.check.absorb(task, extra, db, rows, today)
        department.checks.append(result)
        department.messages += department.check.notices(result, task, extra, db, today)
    for pending in pending_questions(db):
        if pending.kind == "task":
            spec = extras[pending.task_id].spec if pending.task_id in extras else ""
            reply = department.assistant.answer(pending.task_id, spec, pending.question)
            quote = " | ".join(reply.quotes) if reply.quotes else None
            post(db, pending.thread_id, ASSISTANT, reply.text(), today, quote)
    department.messages += chat_notices(db, team.org, today, team)
    department.waiting = [
        {"thread": p.thread_id, "task": p.task_id, "since": p.question.day.isoformat()}
        for p in pending_questions(db)
    ]


def _settle_calls(department: _Department, calls_api: CallsApi) -> None:
    """Ask the provider about calls whose outcome was not known yet; some report late."""
    for key in pending_calls(department.db):
        try:
            found = calls_api.find(key)
        except ApiError:
            continue  # the provider is down: it stays pending and the next run asks again
        if found and found.get("answered_by"):
            settle_result(department.db, key, AnsweredBy(found["answered_by"]))


def _deliver(
    departments: Mapping[str, _Department], org, calls_api: CallsApi, team: TeamSettings, today
) -> int:
    undelivered = 0
    for name in sorted(departments):
        department = departments[name]
        by_id = {m.message_id: m for m in department.messages}
        for message in sorted(by_id.values(), key=lambda m: (_depth(m, by_id), m.message_id)):
            if message.after and not _sent(department.store, message.after):
                continue  # a reminder waits for its quiz, a call for its reminder
            person = org.person(message.recipient)
            call = message.channel is Channel.CALL
            purpose = team.call_purpose if message.kind is Kind.QUIZ_CALL else "reminder"
            if call:
                calls_api.extras[message.message_id] = _call_extras(
                    department, message, person.id, purpose, team
                )
            action = to_action(message, person.phone if call else person.name)
            delivery = (department.calls if call else department.outbox).deliver(action)
            if delivery.status in _UNSETTLED:
                undelivered += 1
            elif call and delivery.status in ("sent", "reconciled"):
                found = calls_api.find(message.message_id)
                answered = (
                    AnsweredBy(found.get("answered_by") or "pending")
                    if found
                    else AnsweredBy.PENDING
                )
                record_result(
                    department.db,
                    message.message_id,
                    person.id,
                    message.task_id,
                    answered,
                    purpose,
                    today,
                )
    return undelivered


def _call_extras(
    department: _Department, message: Message, person: str, purpose: str, team: TeamSettings
) -> dict[str, Any]:
    """What a voice provider needs beyond the words: why it calls, what to ask, where to answer."""
    extras: dict[str, Any] = {"purpose": purpose}
    if team.calls_callback_url:
        extras["reply_to"] = team.calls_callback_url
    if purpose == "verification":
        quiz = open_quiz(department.db, message.task_id, person)
        if quiz is not None:
            extras["questions"] = [
                {"number": n, "prompt": q.prompt}
                for n, q in enumerate(quiz.questions, start=1)
                if q.graded
            ]
    return extras


def _depth(message: Message, by_id: Mapping[str, Message]) -> int:
    depth = 0
    while message.after and message.after in by_id:
        message, depth = by_id[message.after], depth + 1
    return depth


def _sent(store: Store, message_id: str) -> bool:
    stored = store.get_action(message_id)
    return stored is not None and stored.status == "sent"


def _write_artefact(path: Path, settings: Settings, departments: Mapping[str, _Department]) -> None:
    """What was decided, sorted, with no delivery status and no level or score."""
    content = {
        "date": settings.today.isoformat(),
        "departments": {
            name: {
                "checks": sorted(
                    (
                        {
                            "task": c.task_id,
                            "person": c.person,
                            "status": c.status,
                            "quiz": c.quiz_id,
                        }
                        for c in department.checks
                    ),
                    key=lambda c: c["task"],
                ),
                "messages": sorted(
                    (
                        {
                            "id": m.message_id,
                            "kind": m.kind.value,
                            "task": m.task_id,
                            "to": m.recipient,
                            "channel": m.channel.value,
                        }
                        for m in {m.message_id: m for m in department.messages}.values()
                    ),
                    key=lambda m: m["id"],
                ),
                "waiting": sorted(department.waiting, key=lambda w: w["thread"]),
            }
            for name, department in sorted(departments.items())
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, indent=2, sort_keys=True) + "\n", encoding="utf-8")
