"""The completion check: when a task is Done, ask the person what it required.

Two phases, so a rerun cannot change an outcome. ``absorb`` writes what is new: it issues the
quiz once, grades the answers that came in, and puts each settled quiz on the trust ledger once.
``notices`` only reads the result and builds messages, which are the same on every run of the
same state. The size of the quiz comes from the person's level: the stricter the record, the more
questions, and a halted person gets a human instead of a quiz.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from followup.guard import screen_text
from followup.llm import LLMGateway
from followup.models import Status, Task

from .calls import AnsweredBy, can_call, ladder
from .calls import results as call_results
from .check import ModelAudit
from .extras import TaskExtras
from .grading import Grade, Grader, answers_for, quiz_verdict
from .notices import Channel, HistoryRow, Kind, Message, issuer_of
from .org import Capability, Org, Person, Role
from .pins import keep, pinned
from .quiz import Question, Quiz, QuizMaker, issue, open_quiz, select
from .settings import TeamSettings
from .trace import TokenTrace, TraceError, propose_tokens, trace
from .trust import Event, Ledger, Level, plan_for, standing

Status_ = Literal["skipped", "halted", "issued", "pending", "pass", "fail", "needs_review"]
MODEL_FEATURES = ("quiz_questions", "grade_answer", "spec_tokens", "assist_answer")


@dataclass(frozen=True)
class CheckResult:
    task_id: str
    person: str | None
    status: Status_
    quiz_id: str | None = None
    traces: tuple[TokenTrace, ...] = ()
    flags: tuple[str, ...] = ()
    reason: str = ""


def trace_questions(traces: tuple[TokenTrace, ...]) -> list[Question]:
    """One question per name the spec asked for and the code check did not find. Not scored."""
    return [
        Question(
            "t" + hashlib.sha256(item.token.encode("utf-8")).hexdigest()[:6],
            f'Where in the code is "{item.token}" handled? The task names it and it was not found.',
            item.token,
            (),
            "trace",
            graded=False,
        )
        for item in traces
        if item.missing
    ]


class CompletionCheck:
    def __init__(
        self,
        org: Org,
        team: TeamSettings,
        trust: Ledger,
        quizzes: QuizMaker,
        grader: Grader,
        gateway: LLMGateway,
        audit: ModelAudit,
        code_root: Path,
    ) -> None:
        self.org = org
        self.team = team
        self.trust = trust
        self.quizzes = quizzes
        self.grader = grader
        self.gateway = gateway
        self.audit = audit
        self.code_root = code_root

    # -- phase 1: write what is new ------------------------------------------------------

    def absorb(
        self,
        task: Task,
        extras: TaskExtras | None,
        db: sqlite3.Connection,
        rows: list[HistoryRow],
        today: date,
    ) -> CheckResult:
        owner = self.org.by_name(task.owner)
        issuer = issuer_of(task, extras, self.org)
        who = owner.id if owner else None
        fresh_close = any(
            r.field == "status" and r.value == Status.DONE.value and not r.on_activation
            for r in rows
        )
        if owner is None or issuer is None or extras is None or not extras.spec:
            return CheckResult(task.task_id, who, "skipped", reason="no spec, owner or issuer")
        if task.status is not Status.DONE:
            return CheckResult(task.task_id, who, "skipped", reason="not done")
        quiz = open_quiz(db, task.task_id, owner.id)
        if quiz is not None:
            return self._settle(quiz, task, owner, db, today)
        if not fresh_close:
            return CheckResult(task.task_id, who, "skipped", reason="done before the layer started")
        return self._issue(task, owner, issuer, extras, db, today)

    def _issue(
        self,
        task: Task,
        owner: Person,
        issuer: Person,
        extras: TaskExtras,
        db: sqlite3.Connection,
        today: date,
    ) -> CheckResult:
        current = standing(self.trust.events(owner.id), today, self.team)
        if current.level is Level.HALTED:
            return CheckResult(task.task_id, owner.id, "halted", reason="halted for review")
        seed = f"{owner.id}|{task.task_id}"
        plan = plan_for(current, seed, self.team)
        if plan is None:
            return CheckResult(task.task_id, owner.id, "skipped", reason="spot check not due")
        flagged = bool(screen_text(extras.spec))
        flags = ["spec_flagged"] if flagged else []
        candidates = self.quizzes.candidates(task.task_id, extras.spec, flagged)
        if not candidates:
            return CheckResult(task.task_id, owner.id, "skipped", reason="no questions possible")
        questions = select(candidates, plan, seed)
        traces: tuple[TokenTrace, ...] = ()
        if owner.role is Role.ENGINEER and extras.code_path:
            try:
                tokens = propose_tokens(
                    task.task_id, extras.spec, self.gateway, db, self.audit, flagged
                )
                limit = plan.tokens if plan.tokens is not None else len(tokens)
                traces = tuple(trace(tokens[:limit], self.code_root, extras.code_path))
            except TraceError:
                flags.append("trace_failed")
        questions += trace_questions(traces)
        quiz = issue(
            db,
            task_id=task.task_id,
            person=owner.id,
            issuer=issuer.id,
            spec=extras.spec,
            level=current.level.value,
            questions=questions,
            day=today,
        )
        keep(
            db,
            "trace",
            quiz.quiz_id,
            {"results": [{"token": t.token, "found_in": list(t.found_in)} for t in traces]},
            "rules",
            today,
        )
        return CheckResult(task.task_id, owner.id, "issued", quiz.quiz_id, traces, tuple(flags))

    def _settle(
        self, quiz: Quiz, task: Task, owner: Person, db: sqlite3.Connection, today: date
    ) -> CheckResult:
        answers = answers_for(db, quiz.quiz_id)
        grades: list[Grade] = []
        flags: set[str] = set()
        for question in quiz.questions:
            if question.graded and question.question_id in answers:
                grade = self.grader.grade(quiz, question, answers[question.question_id])
                grades.append(grade)
                if grade.flagged:
                    flags.add("answer_flagged")
        review = _review(db, quiz.quiz_id)
        verdict = quiz_verdict(quiz, grades, review, self.team.quiz_pass_score)
        traces = _stored_traces(db, quiz.quiz_id)
        if verdict in ("pass", "fail"):
            self.trust.record(Event(owner.id, quiz.quiz_id, "quiz", verdict == "pass", today))
            if review is not None:
                self._judge_the_grader(quiz, grades, review)
            status: Status_ = verdict
        elif verdict == "needs_review":
            status = "needs_review"
        else:
            status = "issued" if quiz.issued_on == today else "pending"
        return CheckResult(
            task.task_id, owner.id, status, quiz.quiz_id, traces, tuple(sorted(flags))
        )

    def _judge_the_grader(self, quiz: Quiz, grades: list[Grade], accepted: bool) -> None:
        """A person settled a doubt: the grader was right if it said what the person decided."""
        doubted = [g for g in grades if g.verdict == "needs_review" and g.model_pass is not None]
        if len(doubted) == 1:
            self.audit.record(
                "grade_answer", quiz.quiz_id, "review", doubted[0].model_pass == accepted
            )

    # -- phase 2: build messages from the state ------------------------------------------

    def notices(
        self,
        result: CheckResult,
        task: Task,
        extras: TaskExtras | None,
        db: sqlite3.Connection,
        today: date,
    ) -> list[Message]:
        owner = self.org.by_name(task.owner)
        issuer = issuer_of(task, extras, self.org)
        if owner is None or issuer is None:
            return []
        base = {"task_id": task.task_id, "title": task.title, "assignee": owner.name}
        out: list[Message] = []
        if result.status == "halted":
            reviewer = (self.org.chain_above(issuer.id) or (issuer,))[0]
            facts = {**base, "assignee_id": owner.id, "reviewer_id": reviewer.id}
            out.append(
                Message(
                    Kind.HUMAN_REVIEW, task.task_id, reviewer.id, Channel.MESSAGE, owner.id, facts
                )
            )
        if result.quiz_id is None:
            return out
        quiz = open_quiz(db, task.task_id, owner.id)
        assert quiz is not None
        if result.status in ("issued", "pending"):
            out += self._rungs(quiz, owner, issuer, base, db, today)
        elif result.status in ("pass", "fail"):
            out.append(self._verdict(result, owner, issuer, base, today))
        elif result.status == "needs_review":
            facts = {**base, "quiz_id": quiz.quiz_id, "issuer_id": issuer.id}
            out.append(
                Message(
                    Kind.NEEDS_REVIEW, task.task_id, issuer.id, Channel.MESSAGE, quiz.quiz_id, facts
                )
            )
        return out

    def model_notices(self) -> list[Message]:
        """Tell the boss, once per model and feature, that a check now runs on rules."""
        boss = self.org.with_role(Role.BOSS)[0]
        return [
            Message(
                Kind.RULES_ONLY,
                "MODEL",
                boss.id,
                Channel.MESSAGE,
                f"{self.audit.model}:{feature}",
                {"feature": feature, "model": self.audit.model},
            )
            for feature in MODEL_FEATURES
            if self.audit.rules_only(feature)
        ]

    def _rungs(
        self,
        quiz: Quiz,
        owner: Person,
        issuer: Person,
        base: dict[str, str],
        db: sqlite3.Connection,
        today: date,
    ) -> list[Message]:
        outcomes = call_results(db)
        out: list[Message] = []
        previous: str | None = None
        lines = [
            f"{n}. {q.prompt}" + ("" if q.graded else " (not scored)")
            for n, q in enumerate(quiz.questions, start=1)
        ]
        facts = {**base, "assignee_id": owner.id, "questions": "\n".join(lines)}
        rungs = ladder(
            quiz.issued_on,
            today,
            self.team.ladder_step_days,
            can_call(owner, self.team),
            "verification",
        )
        for rung in rungs:
            message = Message(
                rung.kind, base["task_id"], owner.id, rung.channel, quiz.quiz_id, facts, previous
            )
            out.append(message)
            previous = message.message_id
            outcome = outcomes.get(message.message_id)
            if rung.channel is Channel.CALL and outcome and outcome[0] is not AnsweredBy.PERSON:
                how = "no answer" if outcome[0] is AnsweredBy.NONE else "voicemail"
                reached = {**base, "recipient_name": owner.name, "answered_by": how}
                out.append(
                    Message(
                        Kind.UNREACHED,
                        base["task_id"],
                        issuer.id,
                        Channel.MESSAGE,
                        message.message_id,
                        reached,
                    )
                )
        return out

    def _verdict(
        self, result: CheckResult, owner: Person, issuer: Person, base: dict[str, str], today: date
    ) -> Message:
        detail = []
        if result.flags:
            detail.append("Flags: " + ", ".join(result.flags) + ".")
        missing = [t.token for t in result.traces if t.missing]
        if missing:
            detail.append("Not found in the code: " + ", ".join(missing) + ".")
        if self.team.trust_visibility == "issuers" and self.org.can(
            issuer.id, Capability.SEE_TRUST
        ):
            level = standing(self.trust.events(owner.id), today, self.team).level.value
            detail.append(f"Check level now: {level}.")
        facts = {
            **base,
            "verdict": "passed" if result.status == "pass" else "failed",
            "detail": "".join(f" {d}" for d in detail),
        }
        return Message(
            Kind.VERDICT, base["task_id"], issuer.id, Channel.MESSAGE, result.quiz_id or "", facts
        )


def _review(db: sqlite3.Connection, quiz_id: str) -> bool | None:
    row = db.execute("SELECT accepted FROM reviews WHERE quiz_id = ?", (quiz_id,)).fetchone()
    return None if row is None else bool(row[0])


def _stored_traces(db: sqlite3.Connection, quiz_id: str) -> tuple[TokenTrace, ...]:
    stored = pinned(db, "trace", quiz_id)
    if not stored:
        return ()
    return tuple(TokenTrace(r["token"], tuple(r["found_in"])) for r in stored["results"])
