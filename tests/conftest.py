"""Builders for raw records and tasks, shared by the test modules."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from followup.llm import Completion, LLMRequest
from followup.llm.stub import StubClient
from followup.models import Task
from followup.normalize import normalise_task
from followup.retry import RetryPolicy

TODAY = date(2026, 10, 21)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "tasks.json"


def raw_task(**overrides: Any) -> dict[str, Any]:
    """A healthy, on-track record; tests override only what they are about."""
    record: dict[str, Any] = {
        "task_id": "TC-2001",
        "project": "SURF Illinois",
        "task": "Fiber splicing",
        "assigned_to": "John Smith",
        "due_date": "2026-10-30",
        "status": "Working on it",
        "last_update": "2026-10-20",
        "notes": "On schedule.",
    }
    record.update(overrides)
    return record


def make_task(**overrides: Any) -> Task:
    return normalise_task(raw_task(**overrides))


@pytest.fixture
def fixture_records() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class ScriptedLLM:
    """LLM client that replies from a script and records every request it receives."""

    model = "scripted-model"

    def __init__(self, *replies: str | Exception, default: str | Exception | None = None):
        self.replies = list(replies)
        self.default = default
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> Completion:
        self.requests.append(request)
        reply = self.replies.pop(0) if self.replies else self.default
        if reply is None:
            raise AssertionError(f"unexpected {request.kind} request")
        if isinstance(reply, Exception):
            raise reply
        return Completion(reply, input_tokens=100, output_tokens=20)


class RecordingStub(StubClient):
    """The deterministic stub, plus a record of what it was asked."""

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> Completion:
        self.requests.append(request)
        return super().complete(request)


class Sleeps(list):
    """Stands in for ``time.sleep`` and remembers the delays."""

    def __call__(self, seconds: float) -> None:
        self.append(seconds)


def no_wait_policy(sleeps: Sleeps | None = None, **overrides: Any) -> RetryPolicy:
    return RetryPolicy(
        sleep=sleeps if sleeps is not None else Sleeps(), jitter=lambda: 0.0, **overrides
    )
