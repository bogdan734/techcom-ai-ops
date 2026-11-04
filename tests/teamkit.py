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
