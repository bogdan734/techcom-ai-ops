"""What one person sees: their tasks and checks, the tasks they issued, threads, trust.

A read model, built for a viewer. Row filters live here (an assignee sees their own quiz, an
issuer sees their line, the level of a person is never shown to that person), and the
visibility setting decides whether levels exist for anyone at all. The same structure feeds the
console report and the app.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from followup.models import Status, Task

from . import tasks as tasks_module
from .brief import structure
from .chat import ASSISTANT, is_closed, members_of, posts_of, waiting_question
from .context import Context, read_all
from .grading import Grade, quiz_verdict
from .notices import issuer_of
from .org import Capability, Person, Role
from .pins import pinned
from .quiz import Quiz, open_quiz
from .schema import CONTROL
from .shared import Shared, shared_tasks
from .trust import Event, Ledger, standing


@dataclass(frozen=True)
class QuestionView:
    number: int
    question_id: str
    prompt: str
    graded: bool
    answer: str | None


@dataclass(frozen=True)
class CheckView:
    task_id: str
    title: str
    project: str
    due: str
    task_status: str
    assignee: str
    issuer: str
    status: str  # none | open | answered | needs_review | pass | fail
    quiz_id: str | None = None
    issued_on: str | None = None
    questions: tuple[QuestionView, ...] = ()
    missing: tuple[str, ...] = ()
    can_answer: bool = False
    can_review: bool = False
    assignee_id: str = ""
    spec: str = ""
    brief: dict[str, Any] | None = None
    can_accept: bool = False
    can_submit: bool = False
    can_return: bool = False
    # Set when the task was given to several people: how the whole task stands, part by part.
    shared: dict[str, Any] | None = None


@dataclass(frozen=True)
class PostView:
    author: str
    mine: bool
    assistant: bool
    text: str
    day: str
    quoted: bool


@dataclass(frozen=True)
class ThreadView:
    thread_id: str
    kind: str
    task_id: str
    subject: str
    waiting: bool
    closed: bool
    posts: tuple[PostView, ...]


@dataclass(frozen=True)
class TrustView:
    person_id: str
    name: str
    role: str
    level: str
    results: int
    probation: bool
    halted: bool


@dataclass(frozen=True)
class ModelView:
    model: str
    feature: str
    results: int
    pass_rate: float | None
    level: str


@dataclass(frozen=True)
class NoticeView:
    when: str
    to: str
    channel: str
    body: str


@dataclass(frozen=True)
class Dashboard:
    viewer: dict[str, str]
    today: str
    mine: tuple[CheckView, ...]
    issued: tuple[CheckView, ...]
    threads: tuple[ThreadView, ...]
    trust: tuple[TrustView, ...] | None
    models: tuple[ModelView, ...] | None
    notices: tuple[NoticeView, ...]
    assignable: tuple[dict[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_dashboard(ctx: Context, viewer: Person) -> Dashboard:
    org = ctx.org
    issuer_capable = org.can(viewer.id, Capability.ISSUE_TASKS)
    line = org.below(viewer.id)
    moves = tasks_module.events(ctx.vault)
    shared = shared_tasks(ctx.tasks, ctx.extras)
    mine: list[CheckView] = []
    issued: list[CheckView] = []
    for task in sorted(ctx.tasks, key=lambda t: t.task_id):
        owner = org.by_name(task.owner)
        issuer = issuer_of(task, ctx.extras.get(task.task_id), org)
        view = _check_view(ctx, viewer, task, owner, issuer, moves, shared)
        if owner is not None and owner.id == viewer.id:
            mine.append(view)
        in_line = issuer_capable and (
            (issuer is not None and issuer.id == viewer.id)
            or (owner is not None and owner.id in line)
        )
        if in_line:
            issued.append(view)
    return Dashboard(
        viewer={
            "id": viewer.id,
            "name": viewer.name,
            "role": viewer.role.value,
            "department": viewer.department,
            "issuer": "yes" if issuer_capable else "",
        },
        today=ctx.today.isoformat(),
        mine=tuple(mine),
        issued=tuple(issued),
        threads=tuple(_threads(ctx, viewer)),
        trust=_trust(ctx, viewer),
        models=_models(ctx, viewer),
        notices=tuple(_notices(ctx, viewer)),
        assignable=tuple(_assignable(ctx, viewer)),
    )


def explain(ctx: Context, viewer: Person, person_id: str) -> list[Event]:
    """The results behind a level, for someone whose line the person is in; never for oneself."""
    if ctx.team.trust_visibility != "issuers":
        raise PermissionError("trust is hidden in this deployment")
    if person_id == viewer.id or person_id not in ctx.org.below(viewer.id):
        raise PermissionError(f"{viewer.id} may not see the record of {person_id}")
    if not ctx.org.can(viewer.id, Capability.SEE_TRUST):
        raise PermissionError(f"{viewer.id} may not see trust")
    control = ctx.open(CONTROL)
    return Ledger(control, "trust_events").events(person_id) if control else []


def to_markdown(board: Dashboard) -> str:
    out = [f"# {board.viewer['name']} ({board.viewer['role']}), {board.today}", ""]
    for title, checks in (("My tasks", board.mine), ("Tasks I issued", board.issued)):
        out += [f"## {title}", ""]
        out += [_line(c) for c in checks] or ["Nothing."]
        out.append("")
    out += ["## Threads", ""]
    out += [
        f"- {t.task_id} {t.subject}: {'waiting for an answer' if t.waiting else 'answered'}"
        for t in board.threads
    ] or ["Nothing."]
    if board.trust is not None:
        out += ["", "## Check levels in my line", ""]
        out += [
            f"- {t.name} ({t.role}): {t.level}, {t.results} result(s)" for t in board.trust
        ] or ["Nobody."]
    if board.models is not None:
        out += ["", "## Model record", ""]
        out += [
            f"- {m.model} {m.feature}: {m.results} result(s), "
            f"{'n/a' if m.pass_rate is None else f'{m.pass_rate:.0%}'} passed, {m.level}"
            for m in board.models
        ] or ["No results yet."]
    out += ["", "## Latest notices", ""]
    out += [f"- {n.when} [{n.channel}] {n.body.splitlines()[0]}" for n in board.notices] or [
        "None."
    ]
    return "\n".join(out) + "\n"


def _line(check: CheckView) -> str:
    extra = f"; not found in the code: {', '.join(check.missing)}" if check.missing else ""
    whole = (
        f", shared: {check.shared['done']} of {check.shared['total']} handed in"
        if check.shared
        else ""
    )
    return (
        f"- {check.task_id} {check.title} ({check.task_status}{whole}): check {check.status}{extra}"
    )


# -- builders ------------------------------------------------------------------------------


def _assignable(ctx: Context, viewer: Person) -> list[dict[str, str]]:
    """Whom an issuer may give a task to: people in their line who can act."""
    if not ctx.org.can(viewer.id, Capability.ISSUE_TASKS):
        return []
    people = (ctx.org.person(pid) for pid in sorted(ctx.org.below(viewer.id)))
    return [
        {"id": p.id, "name": p.name, "role": p.role.value, "department": p.department}
        for p in people
        if p.role not in (Role.PROJECT_MANAGER, Role.WAREHOUSE, Role.SUPPORT)
    ]


def _check_view(
    ctx: Context,
    viewer: Person,
    task: Task,
    owner: Person | None,
    issuer: Person | None,
    moves: list[tasks_module.TaskEvent],
    shared: Mapping[str, Shared],
) -> CheckView:
    own = owner is not None and owner.id == viewer.id
    is_issuer = issuer is not None and issuer.id == viewer.id
    in_line = is_issuer or (owner is not None and owner.id in ctx.org.below(viewer.id))
    extra = ctx.extras.get(task.task_id)
    last = tasks_module.last_event(moves, task.task_id)
    done = task.status is Status.DONE
    shown = bool(extra and extra.spec and (own or in_line))
    base = {
        "task_id": task.task_id,
        "title": task.title,
        "project": task.project,
        "due": task.due_date.isoformat() if task.due_date else "",
        "task_status": task.status.value,
        "assignee": owner.name if owner else "unassigned",
        "issuer": issuer.name if issuer else "",
        "assignee_id": owner.id if owner else "",
        "spec": extra.spec if shown and extra else "",
        "brief": structure(extra.spec).to_dict() if shown and extra else None,
        "can_accept": own
        and not done
        and (last is None or last.kind != "accepted")
        and task.status in (Status.NOT_STARTED, Status.BLOCKED, Status.UNKNOWN),
        "can_submit": own and not done,
        "can_return": is_issuer and done,
        "shared": _shared_view(shared.get(extra.group) if extra and extra.group else None),
    }
    db = ctx.open(ctx.department(task))
    quiz = open_quiz(db, task.task_id, owner.id) if db is not None and owner else None
    if quiz is None:
        return CheckView(status="none", **base)
    assert db is not None
    answers = dict(
        read_all(db, "SELECT question_id, answer FROM answers WHERE quiz_id = ?", (quiz.quiz_id,))
    )
    status = _status(ctx, db, quiz, answers)
    may_see_answers = own or issuer is not None or ctx.org.can(viewer.id, Capability.ISSUE_TASKS)
    questions = tuple(
        QuestionView(
            n,
            q.question_id,
            q.prompt,
            q.graded,
            answers.get(q.question_id) if may_see_answers else None,
        )
        for n, q in enumerate(quiz.questions, start=1)
    )
    traced = (pinned(db, "trace", quiz.quiz_id) or {}).get("results", [])
    return CheckView(
        status=status,
        quiz_id=quiz.quiz_id,
        issued_on=quiz.issued_on.isoformat(),
        questions=questions,
        missing=tuple(r["token"] for r in traced if not r["found_in"]),
        can_answer=own and status == "open",
        can_review=in_line
        and ctx.org.can(viewer.id, Capability.ISSUE_TASKS)
        and status == "needs_review",
        **base,
    )


def _shared_view(group: Shared | None) -> dict[str, Any] | None:
    if group is None:
        return None
    return {
        "group": group.group,
        "status": group.status.value,
        "done": group.done,
        "total": len(group.parts),
        "parts": [
            {"task_id": p.task_id, "assignee": p.owner or "unassigned", "status": p.status.value}
            for p in group.parts
        ],
    }


def _status(ctx: Context, db, quiz: Quiz, answers: dict[str, str]) -> str:
    asked = [q for q in quiz.questions if q.graded]
    if any(q.question_id not in answers for q in asked):
        return "open"
    grades = []
    for q in asked:
        stored = pinned(db, "grade", f"{quiz.quiz_id}|{q.question_id}")
        if stored is None:
            return "answered"  # graded by the next run
        grades.append(Grade(**stored))
    review = read_all(db, "SELECT accepted FROM reviews WHERE quiz_id = ?", (quiz.quiz_id,))
    verdict = quiz_verdict(
        quiz, grades, bool(review[0][0]) if review else None, ctx.team.quiz_pass_score
    )
    return "answered" if verdict == "pending" else verdict


def _threads(ctx: Context, viewer: Person) -> list[ThreadView]:
    found: list[ThreadView] = []
    for name in ctx.vault.departments():
        db = ctx.open(name)
        rows = read_all(
            db,
            "SELECT thread_id, kind, COALESCE(task_id, ''), subject "
            "FROM threads ORDER BY thread_id",
        )
        for thread_id, kind, task_id, subject in rows:
            members = members_of(db, thread_id)
            if viewer.id not in members:
                continue
            posts = posts_of(db, thread_id)
            found.append(
                ThreadView(
                    thread_id,
                    kind,
                    task_id,
                    subject,
                    waiting_question(posts, members) is not None and not is_closed(db, thread_id),
                    is_closed(db, thread_id),
                    tuple(
                        PostView(
                            _name(ctx, p.author),
                            p.author == viewer.id,
                            p.author == ASSISTANT,
                            p.text,
                            p.day.isoformat(),
                            bool(p.quote),
                        )
                        for p in posts
                    ),
                )
            )
    return found


def _trust(ctx: Context, viewer: Person) -> tuple[TrustView, ...] | None:
    team = ctx.team
    if team.trust_visibility != "issuers" or not ctx.org.can(viewer.id, Capability.SEE_TRUST):
        return None
    control = ctx.open(CONTROL)
    ledger = Ledger(control, "trust_events") if control else None
    views = []
    for pid in sorted(ctx.org.below(viewer.id)):  # never the viewer's own record
        person = ctx.org.person(pid)
        events = ledger.events(pid) if ledger else []
        now = standing(events, ctx.today, team)
        views.append(
            TrustView(
                pid,
                person.name,
                person.role.value,
                now.level.value,
                now.events,
                now.probation,
                now.level.value == "halted",
            )
        )
    return tuple(views)


def _models(ctx: Context, viewer: Person) -> tuple[ModelView, ...] | None:
    if not ctx.org.can(viewer.id, Capability.ISSUE_TASKS):
        return None
    control = ctx.open(CONTROL)
    if control is None:
        return ()
    rows = read_all(control, "SELECT DISTINCT subject FROM model_events ORDER BY subject")
    ledger = Ledger(control, "model_events")
    views = []
    for (subject,) in rows:
        events = ledger.events(subject)
        model, _, feature = subject.partition(":")
        now = standing(events, ctx.today, ctx.team)
        rate = sum(e.passed for e in events) / len(events) if events else None
        views.append(ModelView(model, feature, len(events), rate, now.level.value))
    return tuple(views)


def _notices(ctx: Context, viewer: Person, limit: int = 25) -> list[NoticeView]:
    """Messages and calls delivered to the viewer, with the evaluation date they went out on."""
    found: list[tuple[str, int, NoticeView]] = []
    for name in ctx.vault.departments():
        db = ctx.open(name)
        for address, channel in ((viewer.name, "message"), (viewer.phone, "call")):
            if address is None:
                continue
            rows = read_all(
                db,
                "SELECT COALESCE(sent_on, substr(updated_at, 1, 10)), rowid, message FROM actions "
                "WHERE status = 'sent' AND target = ?",
                (address,),
            )
            found += [
                (day, rowid, NoticeView(day, address, channel, body)) for day, rowid, body in rows
            ]
    found.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [notice for _, _, notice in found[:limit]]


def _name(ctx: Context, author: str) -> str:
    return "Assistant" if author == ASSISTANT else ctx.org.person(author).name
