"""Builders for raw records and tasks, shared by the test modules."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from followup.models import Task
from followup.normalize import normalise_task

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
