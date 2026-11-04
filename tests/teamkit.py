"""Builders shared by the team tests: an org on disk, settings from it, a fake model."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from followup.llm import Completion, LLMRequest
from followup.team.settings import TeamSettings


def sample_people() -> list[dict[str, Any]]:
    """boss -> senior -> manager -> engineer and field worker; one support agent; an outsider."""
    return [
        {"id": "bea", "name": "Bea Boss", "role": "boss", "department": "management"},
        {
            "id": "sid",
            "name": "Sid Senior",
            "role": "senior_manager",
            "department": "management",
            "reports_to": "bea",
        },
        {
            "id": "max",
            "name": "Max Manager",
            "role": "manager",
            "department": "management",
            "reports_to": "sid",
        },
        {
            "id": "eve",
            "name": "Eve Engineer",
            "role": "engineer",
            "department": "engineering",
            "reports_to": "max",
            "phone": "+15550000001",
            "call_consent": True,
        },
        {
            "id": "finn",
            "name": "Finn Field",
            "role": "field",
            "department": "field-ops",
            "reports_to": "max",
            "phone": "+15550000002",
        },
        {
            "id": "sue",
            "name": "Sue Support",
            "role": "support",
            "department": "support",
            "reports_to": "sid",
        },
    ]


def write_org(tmp: Path, people: Sequence[Mapping[str, Any]] | None = None) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / "org.json"
    path.write_text(json.dumps({"people": list(people or sample_people())}), encoding="utf-8")
    return path


def team_settings(tmp: Path, people: Sequence[Mapping[str, Any]] | None = None, **env: str):
    """Settings the way the CLI builds them: from an environment mapping."""
    values = {"ORG_FILE": str(write_org(tmp, people)), "CALL_TIME": "10:00", **env}
    settings = TeamSettings.from_env(values)
    assert settings is not None
    return settings


class KindLLM:
    """Fake model with one script per request kind; the last reply of a script repeats."""

    def __init__(self, script: Mapping[str, Sequence[str | Exception]] | None = None, model="fake"):
        self.model = model
        self.script = {kind: list(replies) for kind, replies in (script or {}).items()}
        self.requests: dict[str, list[LLMRequest]] = defaultdict(list)

    def complete(self, request: LLMRequest) -> Completion:
        self.requests[request.kind].append(request)
        replies = self.script.get(request.kind)
        if not replies:
            raise AssertionError(f"unexpected {request.kind} request")
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            raise reply
        return Completion(reply, input_tokens=100, output_tokens=20)


def make_vault(tmp: Path, visibility: str = "issuers", names: Sequence[str] | None = None):
    """A vault over the sample org with every file created (or just ``names``)."""
    from followup.team.org import Org
    from followup.team.vault import Vault

    vault = Vault(tmp / "state", Org.from_records(sample_people()), visibility)
    for name in names if names is not None else vault.known():
        vault.create(name)
    return vault


SPEC = (
    "Install four handholes along Elm Street. "
    "The crew must record GPS coordinates for each handhole. "
    "Splice loss should stay under 0.5 dB."
)


def world_records(status="Working on it", more=()):
    """Eve (engineer) has a task with a spec; Finn (field) is blocked and gets a follow-up."""
    from conftest import raw_task

    return [
        raw_task(
            task_id="TC-1",
            assigned_to="Eve Engineer",
            status=status,
            spec=SPEC,
            assigned_by="max",
            due_date="2026-10-30",
            notes="On schedule.",
        ),
        raw_task(
            task_id="TC-2",
            assigned_to="Finn Field",
            status="Blocked",
            last_update="2026-10-15",
            notes="Awaiting materials from warehouse; delivery date unknown.",
            due_date="2026-10-18",
        ),
        *more,
    ]


def world_settings(tmp: Path, records, today="2026-10-21", **env):
    """Base settings with an org file, the way the CLI builds them."""
    from followup.config import Settings

    (tmp / "tasks.json").write_text(json.dumps(records))
    values = {
        "TASKS_FILE": str(tmp / "tasks.json"),
        "OUT_DIR": str(tmp / "out"),
        "EVALUATION_DATE": today,
        "ORG_FILE": str(write_org(tmp / "cfg")),
        "CALL_TIME": "10:00",
        "AUDIT_RATE": "0",
        **env,
    }
    return Settings.from_env(values)


def run_day(tmp: Path, records, today="2026-10-21", faults=None, **env):
    from followup.pipeline import run

    return run(world_settings(tmp, records, today, **env), faults)


def world_context(tmp: Path, records, today="2026-10-21", **env):
    from followup.team.context import load_context

    return load_context(world_settings(tmp, records, today, **env))
