"""Changes a person makes: answer a check, settle a doubt, clear a halt, write in a thread.

Each action checks who is asking, then writes through a connection that holds only the tables
the role may write. Nothing here sends: the notices these changes cause are built and delivered
by the next run, under the run lock. The console and the app call the same functions.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from followup.guard import screen_text

from .access import CHAT_TABLES
from .chat import open_thread, post
from .context import ActionError, Context, read_all
from .dashboard import _status
from .notices import issuer_of
from .org import SUPPORT_DEPARTMENT, Capability, Person, Role
from .quiz import open_quiz
from .schema import CONTROL
from .trust import REVIEW_CLEARED, Event, Ledger, Level, standing

MAX_TEXT = 2000


def submit_answers(ctx: Context, viewer: Person, task_id: str, answers: Mapping[int, str]) -> int:
    """All scored answers of one quiz in one go; the quiz belongs to the person under check."""
    task = ctx.task(task_id)
    department = ctx.department(task)
    quiz = _quiz(ctx, task_id, viewer.id, department, "no check was issued to you for this task")
    scored = {n: q for n, q in enumerate(quiz.questions, start=1) if q.graded}
    unknown = sorted(set(answers) - set(scored))
    if unknown:
        raise ActionError(f"there is no scored question {unknown[0]}")
    blank = [n for n in scored if not _text(answers.get(n, ""))]
    if blank:
        raise ActionError(f"answer every scored question; missing {', '.join(map(str, blank))}")
    db = ctx.vault.open(department, viewer, frozenset({"answers"}))
    try:
        if db.execute(
            "SELECT 1 FROM answers WHERE quiz_id = ? LIMIT 1", (quiz.quiz_id,)
        ).fetchone():
            raise ActionError("this check was already answered")
        db.execute("BEGIN")
        for number, question in scored.items():
            text = _text(answers[number])
            db.execute(
                "INSERT INTO answers (quiz_id, question_id, answer, flagged, answered_on) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    quiz.quiz_id,
                    question.question_id,
                    text,
                    int(bool(screen_text(text))),
                    ctx.today.isoformat(),
                ),
            )
        db.execute("COMMIT")
    finally:
        db.close()
    return len(scored)


def review(ctx: Context, viewer: Person, task_id: str, accept: bool, note: str = "") -> str:
    """The issuer (or someone above them) settles a check the grader and the rules split on."""
    task = ctx.task(task_id)
    owner = ctx.org.by_name(task.owner)
    if owner is None:
        raise ActionError("the task has no assignee")
    department = ctx.department(task)
    quiz = _quiz(ctx, task_id, owner.id, department, "no check was issued for this task")
    issuer = issuer_of(task, ctx.extras.get(task.task_id), ctx.org)
    in_line = (issuer is not None and issuer.id == viewer.id) or owner.id in ctx.org.below(
        viewer.id
    )
    if not (in_line and ctx.org.can(viewer.id, Capability.ISSUE_TASKS)):
        raise ActionError(f"{viewer.id} may not review the check of {owner.id}")
    service = ctx.open(department)
    answers = dict(
        read_all(
            service, "SELECT question_id, answer FROM answers WHERE quiz_id = ?", (quiz.quiz_id,)
        )
    )
    if _status(ctx, service, quiz, answers) != "needs_review":
        raise ActionError("this check is not waiting for a review")
    db = ctx.vault.open(department, viewer, frozenset({"reviews"}))
    try:
        db.execute(
            "INSERT INTO reviews (quiz_id, reviewer_id, accepted, note, reviewed_on) "
            "VALUES (?, ?, ?, ?, ?)",
            (quiz.quiz_id, viewer.id, int(accept), _text(note, 500), ctx.today.isoformat()),
        )
    finally:
        db.close()
    return quiz.quiz_id


def clear(ctx: Context, viewer: Person, person_id: str) -> None:
    """Lift a halt after talking to the person: the next quiz is back on the standard plan."""
    target = ctx.org.find(person_id)
    if target is None or target.id not in ctx.org.below(viewer.id):
        raise ActionError(f"{person_id} is not in your line")
    if not ctx.org.can(viewer.id, Capability.ISSUE_TASKS):
        raise ActionError(f"{viewer.id} may not clear a halt")
    control = ctx.vault.open(CONTROL)
    try:
        ledger = Ledger(control, "trust_events")
        if standing(ledger.events(target.id), ctx.today, ctx.team).level is not Level.HALTED:
            raise ActionError(f"{target.id} is not halted")
        ledger.record(
            Event(target.id, f"clear-{ctx.today.isoformat()}", REVIEW_CLEARED, True, ctx.today)
        )
    finally:
        control.close()


def say(ctx: Context, viewer: Person, task_id: str, text: str) -> str:
    """Write to the other side of a task, opening the thread if this is the first message."""
    task = ctx.task(task_id)
    owner = ctx.org.by_name(task.owner)
    issuer = issuer_of(task, ctx.extras.get(task.task_id), ctx.org)
    if owner is None or issuer is None:
        raise ActionError("this task has no assignee or issuer to talk to")
    if viewer.id not in (owner.id, issuer.id):
        raise ActionError("only the assignee and the issuer write in a task thread")
    department = ctx.department(task)
    if ctx.open(department) is None:
        raise ActionError("nothing has run yet: start a run first")
    db = ctx.vault.open(department, viewer, CHAT_TABLES)
    try:
        thread = open_thread(
            db,
            kind="task",
            ref=task.task_id,
            subject=task.title,
            opener=viewer.id,
            members={owner.id: "assignee", issuer.id: "issuer"},
            day=ctx.today,
        )
        post(db, thread, viewer.id, _text(text), ctx.today)
    finally:
        db.close()
    return thread


def open_ticket(ctx: Context, viewer: Person, text: str) -> str:
    """A support ticket: a thread in the support department with every support person in it."""
    agents = ctx.org.with_role(Role.SUPPORT)
    if not agents:
        raise ActionError("this org has no support staff")
    if ctx.open(SUPPORT_DEPARTMENT) is None:
        raise ActionError("nothing has run yet: start a run first")
    body = _text(text)
    ref = f"{viewer.id}|{ctx.today.isoformat()}|{hashlib.sha256(body.encode()).hexdigest()[:8]}"
    db = ctx.vault.open(SUPPORT_DEPARTMENT, viewer, CHAT_TABLES)
    try:
        members = {
            viewer.id: "asker",
            **{agent.id: "support" for agent in agents if agent.id != viewer.id},
        }
        thread = open_thread(
            db,
            kind="support",
            ref=ref,
            subject=body[:60],
            opener=viewer.id,
            members=members,
            day=ctx.today,
        )
        post(db, thread, viewer.id, body, ctx.today)
    finally:
        db.close()
    return thread


def reply(ctx: Context, viewer: Person, thread_id: str, text: str) -> None:
    """Add to a thread the viewer is in, wherever it lives."""
    for name in ctx.vault.departments():
        probe = ctx.open(name)
        if probe is not None and read_all(
            probe, "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)
        ):
            try:
                db = ctx.vault.open(name, viewer, CHAT_TABLES)
            except PermissionError as exc:
                raise ActionError(str(exc)) from exc
            try:
                post(db, thread_id, viewer.id, _text(text), ctx.today)
            except PermissionError as exc:
                raise ActionError(str(exc)) from exc
            finally:
                db.close()
            return
    raise ActionError(f"no thread {thread_id}")


def _quiz(ctx: Context, task_id: str, person: str, department: str, missing: str):
    db = ctx.open(department)
    quiz = open_quiz(db, ctx.task(task_id).task_id, person) if db is not None else None
    if quiz is None:
        raise ActionError(missing)
    return quiz


def _text(value: str, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(value).split())
    if len(text) > limit:
        raise ActionError(f"keep it under {limit} characters")
    return text
