"""Messages the team layer sends, each with an id taken from what it is about.

A notice is a pure function of stored state, the inputs and ``today``. Its id covers the kind,
task, recipient, channel and a state key and never the wording or the facts, so a rerun that
words the same event differently is still the same message and the outbox suppresses it. Text
comes from templates: a notice carries facts, so there is nothing for a model to invent.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import string
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Literal

from followup.models import Action, Decision, Status, Task

from .extras import TaskExtras
from .org import Org, Person
from .spec_terms import requirement_sentences, sentences, spec_hash

ACTIVATED = "team_activated_on"


class Channel(StrEnum):
    MESSAGE = "message"
    CALL = "call"


class Kind(StrEnum):
    NEW_TASK = "new_task"
    SPEC_CHANGED = "spec_changed"
    TASK_CLOSED = "task_closed"
    NEXT_TASK = "next_task"
    QUIZ = "quiz"
    QUIZ_REMINDER = "quiz_reminder"
    QUIZ_CALL = "quiz_call"
    VERDICT = "verdict"
    NEEDS_REVIEW = "needs_review"
    HUMAN_REVIEW = "human_review"
    RULES_ONLY = "rules_only"
    CHAT_QUESTION = "chat_question"
    CHAT_ESCALATED = "chat_escalated"
    SUPPORT_TICKET = "support_ticket"
    UNREACHED = "unreached"


_TEMPLATES: Mapping[Kind, str] = {
    Kind.NEW_TASK: (
        "New task {task_id}: {title} ({project}), due {due}. Issued by {issuer}.{requirements}"
    ),
    Kind.SPEC_CHANGED: (
        "The spec of {task_id} ({title}) changed.\nAdded: {added}\nRemoved: {removed}"
    ),
    Kind.TASK_CLOSED: "{assignee} closed {task_id} ({title}).{check}",
    Kind.NEXT_TASK: "{task_id} is closed. {next}",
    Kind.QUIZ: (
        "Completion check for {task_id} ({title}). Answer every scored question: "
        "followup team answer {task_id} --as {assignee_id} 1 'your answer' 2 '...'\n{questions}"
    ),
    Kind.QUIZ_REMINDER: (
        "Reminder: the completion check for {task_id} ({title}) is waiting for answers."
    ),
    Kind.QUIZ_CALL: (
        "This is a reminder from the follow-up service. "
        "The completion check for task {task_id} is waiting for your answers."
    ),
    Kind.VERDICT: "Completion check for {task_id} ({title}) by {assignee}: {verdict}.{detail}",
    Kind.NEEDS_REVIEW: (
        "The checks disagree on {task_id} ({title}) by {assignee}. Decide with: "
        "followup team review {task_id} --as {issuer_id} --accept or --reject"
    ),
    Kind.HUMAN_REVIEW: (
        "{assignee} failed repeated completion checks, so automatic quizzes are halted. "
        "Clear with: followup team clear {assignee_id} --as {reviewer_id}"
    ),
    Kind.RULES_ONLY: (
        "The model's record for {feature} ({model}) fell below the floor; "
        "that check now runs on rules only."
    ),
    Kind.CHAT_QUESTION: "{author} wrote on {task_id}: {text}",
    Kind.CHAT_ESCALATED: "A question on {task_id} from {author} has waited {days} days. {text}",
    Kind.SUPPORT_TICKET: "Support ticket from {author}: {text}",
    Kind.UNREACHED: "Nobody took the call to {recipient_name} about {task_id} ({answered_by}).",
}
_REQUIRED = {
    kind: frozenset(name for _, name, _, _ in string.Formatter().parse(text) if name)
    for kind, text in _TEMPLATES.items()
}


@dataclass(frozen=True)
class Message:
    kind: Kind
    task_id: str
    recipient: str  # person id
    channel: Channel
    state_key: str
    facts: Mapping[str, str] = field(default_factory=dict)
    # The message that has to be sent first (a reminder waits for its quiz); not part of the id.
    after: str | None = None

    def __post_init__(self) -> None:
        missing = _REQUIRED[self.kind] - set(self.facts)
        if missing:
            raise KeyError(f"{self.kind} notice is missing facts {sorted(missing)}")

    @property
    def message_id(self) -> str:
        identity = [
            self.kind.value,
            self.task_id,
            self.recipient,
            self.channel.value,
            self.state_key,
        ]
        raw = json.dumps(identity, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def render(message: Message) -> str:
    return _TEMPLATES[message.kind].format_map(message.facts)


def to_action(message: Message, address: str) -> Action:
    """The outbox record for a message: same id, so the outbox's guarantees carry over."""
    return Action(
        action_id=message.message_id,
        task_id=message.task_id,
        decision=Decision.NOTIFY,
        target=address,
        reason=f"team: {message.kind.value}",
        message=render(message),
        drafted_by="template",
    )


def issuer_of(task: Task, extras: TaskExtras | None, org: Org) -> Person | None:
    """Who issued the task: the named issuer, else the owner's manager."""
    if extras is not None and extras.assigned_by is not None:
        return org.person(extras.assigned_by)
    owner = org.by_name(task.owner)
    return org.person(owner.reports_to) if owner and owner.reports_to else None


# -- history: what we have seen of a task, so "new" and "changed" are facts, not guesses ------


@dataclass(frozen=True)
class HistoryRow:
    task_id: str
    field: Literal["spec", "status"]
    value: str  # the spec hash, or the status
    detail: str  # the spec text, for a diff later
    first_seen: date
    on_activation: bool


def begin_activation(db: sqlite3.Connection, today: date) -> bool:
    """True once per department: the first run, whose history must not read as news."""
    cursor = db.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (ACTIVATED, today.isoformat())
    )
    return cursor.rowcount == 1


def record_history(
    db: sqlite3.Connection,
    task: Task,
    extras: TaskExtras | None,
    today: date,
    activation: bool,
) -> list[HistoryRow]:
    """Append what is new about the task; rows already there are left alone."""
    wanted = [("status", _status(task), "")]
    if extras is not None and extras.spec:
        wanted.append(("spec", spec_hash(extras.spec), extras.spec))
    added = []
    for kind, value, detail in wanted:
        cursor = db.execute(
            "INSERT OR IGNORE INTO task_history "
            "(task_id, field, value, detail, first_seen, on_activation) VALUES (?, ?, ?, ?, ?, ?)",
            (task.task_id, kind, value, detail, today.isoformat(), int(activation)),
        )
        if cursor.rowcount == 1:
            added.append(HistoryRow(task.task_id, kind, value, detail, today, activation))
    return added


def history_for(db: sqlite3.Connection, task_id: str) -> list[HistoryRow]:
    rows = db.execute(
        "SELECT task_id, field, value, detail, first_seen, on_activation FROM task_history "
        "WHERE task_id = ? ORDER BY first_seen, field, value",
        (task_id,),
    ).fetchall()
    return [HistoryRow(t, f, v, d, date.fromisoformat(s), bool(a)) for t, f, v, d, s, a in rows]


def fresh(first: HistoryRow, reference: date | None, today: date, window_days: int) -> bool:
    """Is a task new enough to announce? Its own creation date wins over when we saw it."""
    if reference is not None:
        return 0 <= (today - reference).days <= window_days
    return not first.on_activation and (today - first.first_seen).days <= window_days


def lifecycle_notices(
    task: Task,
    extras: TaskExtras | None,
    rows: Sequence[HistoryRow],
    open_by_owner: Mapping[str, Sequence[Task]],
    org: Org,
    today: date,
    window_days: int,
) -> list[Message]:
    """New task, changed spec, closed task and what is next, for one task."""
    owner = org.by_name(task.owner)
    if owner is None or not rows:
        return []
    issuer = issuer_of(task, extras, org)
    issuer_name = issuer.name if issuer else "your manager"
    specs = sorted((r for r in rows if r.field == "spec"), key=lambda r: (r.first_seen, r.value))
    statuses = [r for r in rows if r.field == "status"]
    first = min(rows, key=lambda r: r.first_seen)
    first_status = min(statuses, key=lambda r: r.first_seen) if statuses else first
    messages: list[Message] = []
    base = {"task_id": task.task_id, "title": task.title}

    if first_status.value != Status.DONE.value and fresh(
        first, extras.created if extras else None, today, window_days
    ):
        due = task.due_date.isoformat() if task.due_date else "no date"
        spec = extras.spec if extras else ""
        points = requirement_sentences(spec)[:3] or sentences(spec)[:1]
        facts = {
            **base,
            "project": task.project,
            "due": due,
            "issuer": issuer_name,
            "requirements": "".join(f"\n- {point}" for point in points),
        }
        messages.append(
            Message(Kind.NEW_TASK, task.task_id, owner.id, Channel.MESSAGE, "new", facts)
        )

    if task.status is not Status.DONE:
        for before, after in zip(specs, specs[1:], strict=False):
            old, new = set(sentences(before.detail)), set(sentences(after.detail))
            facts = {
                **base,
                "added": " | ".join(sorted(new - old)) or "none",
                "removed": " | ".join(sorted(old - new)) or "none",
            }
            for person in {owner.id: owner, **({issuer.id: issuer} if issuer else {})}.values():
                messages.append(
                    Message(
                        Kind.SPEC_CHANGED,
                        task.task_id,
                        person.id,
                        Channel.MESSAGE,
                        after.value,
                        facts,
                    )
                )

    closed = [r for r in statuses if r.value == Status.DONE.value and not r.on_activation]
    if closed and task.status is Status.DONE:
        check = " The completion check follows." if extras and extras.spec else ""
        if issuer is not None:
            facts = {**base, "assignee": owner.name, "check": check}
            messages.append(
                Message(Kind.TASK_CLOSED, task.task_id, issuer.id, Channel.MESSAGE, "done", facts)
            )
        following = _next_open(task, open_by_owner.get(owner.name, ()))
        text = (
            f"Next on your list: {following.task_id} {following.title}, due "
            f"{following.due_date.isoformat() if following.due_date else 'no date'}."
            if following
            else "You have no other open tasks."
        )
        messages.append(
            Message(
                Kind.NEXT_TASK,
                task.task_id,
                owner.id,
                Channel.MESSAGE,
                "done",
                {**base, "next": text},
            )
        )
    return messages


def _next_open(task: Task, candidates: Sequence[Task]) -> Task | None:
    """The earliest due date first, no date last, ties by task id."""
    open_ones = [t for t in candidates if t.status is not Status.DONE and t.task_id != task.task_id]
    return min(
        open_ones,
        key=lambda t: (t.due_date is None, t.due_date or date.max, t.task_id),
        default=None,
    )


def _status(task: Task) -> str:
    return task.status_raw if task.status is Status.UNKNOWN else task.status.value
