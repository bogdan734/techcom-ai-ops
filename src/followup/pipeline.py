"""One run: fetch -> normalise -> decide -> read notes -> draft -> deliver -> report."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .api import ApiError, MessagesApi, TasksApi
from .audit import emit, run_logging
from .config import ConfigError, Settings
from .delivery import Outbox
from .drafting import Drafter
from .guard import INJECTION_FLAG, screen_text
from .llm import LLMClient, LLMGateway, Usage
from .llm.anthropic_client import AnthropicClient
from .llm.stub import StubClient
from .mock_api import MockBackend
from .models import Action, Decision, Task, TaskOutcome, action_id
from .normalize import normalise_all
from .notes import NoteReader
from .retry import CircuitBreaker, RetryPolicy
from .rules import Policy, assess, decide
from .store import Store


@dataclass
class RunReport:
    run_id: str
    today: date
    model: str
    outcomes: list[TaskOutcome] = field(default_factory=list)
    rejected_records: list[str] = field(default_factory=list)
    llm: Usage = field(default_factory=Usage)
    aborted: str | None = None

    @property
    def exit_code(self) -> int:
        """0 all done, 1 run aborted, 2 finished but some follow-ups still owe delivery."""
        if self.aborted:
            return 1
        unsettled = {"failed", "deferred"}
        return 2 if any(o.delivery in unsettled for o in self.outcomes) else 0


class FollowUpService:
    def __init__(
        self,
        today: date,
        policy: Policy,
        store: Store,
        reader: NoteReader,
        drafter: Drafter,
        outbox: Outbox,
    ):
        self.today = today
        self.policy = policy
        self.store = store
        self.reader = reader
        self.drafter = drafter
        self.outbox = outbox

    def process(self, tasks: Sequence[Task]) -> list[TaskOutcome]:
        people = {task.owner for task in tasks if task.owner}
        return [self.handle(task, people) for task in sorted(tasks, key=lambda t: t.task_id)]

    def handle(self, task: Task, people: set[str]) -> TaskOutcome:
        assessment = assess(task, self.today)
        note_flagged, flagged = self._screen(task)
        reading = self.reader.read(task, needed=assessment.needs_reading, flagged=note_flagged)
        verdict = decide(assessment, reading.blocker, self.policy)
        flags = sorted({*reading.flags, *([INJECTION_FLAG] if flagged else [])})
        outcome = TaskOutcome(
            task_id=task.task_id,
            decision=verdict.decision.value,
            target=verdict.target,
            reason=verdict.reason,
            project=task.project,
            task=task.title,
            note_reading={"blocker": reading.blocker.value, "source": reading.source},
            flags=[*flags, *(f"data: {issue}" for issue in task.issues)],
        )
        emit(
            "decision",
            f"{task.task_id}: {verdict.decision.value}"
            + (f" -> {verdict.target}" if verdict.target else "")
            + f" ({verdict.reason})",
            task_id=task.task_id,
            decision=verdict.decision.value,
            target=verdict.target,
            reason=verdict.reason,
            note_reading=outcome.note_reading,
            flags=outcome.flags,
        )
        if verdict.decision is Decision.NO_ACTION or verdict.target is None:
            return outcome
        if reading.source == "unavailable":
            # The note decides who hears about this task; without a trustworthy reading the
            # service holds it rather than acting on a guess it might contradict next run.
            outcome.delivery = "deferred"
            outcome.reason += "; held until the note can be read"
            emit(
                "task_deferred",
                f"{task.task_id}: held until its note can be read",
                level=logging.WARNING,
                task_id=task.task_id,
            )
            return outcome

        outcome.action_id = action_id(task, verdict)
        stored = self.store.get_action(outcome.action_id)
        if stored is not None and stored.status == "sent":
            # Same task state, decision and recipient as a follow-up already delivered.
            emit(
                "duplicate_suppressed",
                f"{task.task_id}: same follow-up already delivered ({stored.message_id})",
                task_id=task.task_id,
                action_id=outcome.action_id,
                message_id=stored.message_id,
            )
            outcome.message = stored.action.message
            outcome.drafted_by = stored.action.drafted_by
            outcome.delivery = "duplicate_suppressed"
            outcome.message_id = stored.message_id
            return outcome

        if stored is not None:
            # Unfinished from an earlier run: resend the same words under the same key.
            message, drafted_by = stored.action.message, stored.action.drafted_by
        else:
            message, drafted_by = self.drafter.draft(task, assessment, verdict, people, flagged)
        delivery = self.outbox.deliver(
            Action(
                action_id=outcome.action_id,
                task_id=task.task_id,
                decision=verdict.decision,
                target=verdict.target,
                reason=verdict.reason,
                message=message,
                drafted_by=drafted_by,
            )
        )
        outcome.message = delivery.action.message
        outcome.drafted_by = delivery.action.drafted_by
        outcome.delivery = delivery.status
        outcome.message_id = delivery.message_id
        return outcome

    def _screen(self, task: Task) -> tuple[bool, bool]:
        """(note flagged, any free text flagged): the title and project reach the model too."""
        note = screen_text(task.notes)
        other = screen_text(f"{task.title}\n{task.project}")
        if note or other:
            emit(
                "text_flagged",
                f"{task.task_id}: free text looks like instructions to the system "
                f"({', '.join(sorted({*note, *other}))}); the model will not see this task",
                level=logging.WARNING,
                task_id=task.task_id,
                patterns=sorted({*note, *other}),
            )
        return bool(note), bool(note or other)


def run(
    settings: Settings,
    faults: Mapping[str, Sequence[str]] | None = None,
    llm: LLMClient | None = None,
    retry: RetryPolicy | None = None,
) -> RunReport:
    """Process every task once, against the in-process mock APIs.

    ``llm`` and ``retry`` default to what the settings describe; tests pass their own.
    """
    llm = llm or make_llm(settings)
    policy = retry or RetryPolicy()
    report = RunReport(uuid.uuid4().hex[:12], settings.today, llm.model)
    out = settings.out_dir
    started = time.monotonic()
    with run_logging(out / "audit.jsonl", report.run_id):
        emit(
            "run_started",
            f"run {report.run_id}: evaluation date {settings.today}, LLM {llm.model}",
            today=settings.today,
            model=llm.model,
            faults=dict(faults or {}),
        )
        backend = MockBackend(
            load_fixture(settings.tasks_file), out / "delivery_log.jsonl", faults=faults
        )
        store = Store(out / "state.db")
        try:
            with backend.client() as http:
                try:
                    records = TasksApi(http, policy).list_tasks()
                except ApiError as exc:
                    report.aborted = str(exc)
                    emit(
                        "run_aborted",
                        f"tasks API unavailable, nothing was sent: {exc}",
                        level=logging.ERROR,
                        error=str(exc),
                    )
                    return report
                tasks, report.rejected_records = normalise_all(records)
                for problem in report.rejected_records:
                    emit("record_rejected", problem, level=logging.WARNING, problem=problem)

                gateway = LLMGateway(llm, settings.llm_max_calls)
                service = FollowUpService(
                    settings.today,
                    settings.policy,
                    store,
                    NoteReader(gateway, store),
                    Drafter(gateway, settings.today),
                    Outbox(store, MessagesApi(http, policy), policy, CircuitBreaker()),
                )
                report.outcomes = service.process(tasks)
                report.llm = gateway.usage
        finally:
            store.close()
        write_decisions(out / "decisions.json", report.outcomes)
        summarise(report, settings, time.monotonic() - started)
    return report


def make_llm(settings: Settings) -> LLMClient:
    if settings.provider == "stub":
        return StubClient()
    return AnthropicClient(settings.model, settings.llm_timeout, settings.llm_max_retries)


def load_fixture(path: Path) -> list[Any]:
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read tasks fixture {path}: {exc}") from exc
    if not isinstance(records, list):
        raise ConfigError(f"tasks fixture {path} must contain a JSON array")
    return records


def write_decisions(path: Path, outcomes: Sequence[TaskOutcome]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(outcome) for outcome in outcomes]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def summarise(report: RunReport, settings: Settings, seconds: float) -> None:
    decisions = Counter(o.decision for o in report.outcomes)
    deliveries = Counter(o.delivery for o in report.outcomes if o.delivery != "none")
    usage = report.llm
    cost = (
        usage.input_tokens * settings.price_input_per_mtok
        + usage.output_tokens * settings.price_output_per_mtok
    ) / 1_000_000
    emit(
        "run_finished",
        f"run {report.run_id} finished in {seconds:.2f}s: {len(report.outcomes)} tasks | "
        f"decisions {_counts(decisions)} | delivery {_counts(deliveries) or 'nothing to send'} | "
        f"LLM {usage.calls} calls, {usage.input_tokens}+{usage.output_tokens} tokens, "
        f"~${cost:.4f}",
        tasks=len(report.outcomes),
        rejected_records=len(report.rejected_records),
        decisions=dict(decisions),
        deliveries=dict(deliveries),
        llm={**asdict(usage), "estimated_cost_usd": round(cost, 6)},
        exit_code=report.exit_code,
        seconds=round(seconds, 3),
    )


def _counts(counter: Counter[str]) -> str:
    return ", ".join(f"{key} {value}" for key, value in sorted(counter.items()))
