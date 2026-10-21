"""Deterministic messages: the fallback whenever a drafted message cannot be used.

They are built only from the facts the rules worked with, so they are always safe to
send; what they lack is the context a model can pick up from the task note.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .wording import days, listing


def render_message(facts: Mapping[str, Any]) -> str:
    ref = _reference(facts)
    decision = facts["decision"]
    if decision == "follow_up_owner":
        return f"Hi {facts['recipient'].split()[0]}, {ref} {_owner_situation(facts)}"
    if decision == "request_eta":
        return (
            f"Hello, {ref} is blocked waiting for materials{_lateness(facts)}. "
            f"Please confirm the expected delivery date so {facts['owner']} can re-plan the work."
        )
    if decision == "escalate":
        reason = facts["reason"][:1].lower() + facts["reason"][1:]
        return (
            f"Hello, escalating {ref}, owned by {facts['owner']}: {reason}. "
            "Please review it and help unblock or re-plan the work."
        )
    if decision == "clarify_missing_info":
        gaps, asks = _gaps(facts)
        return (
            f"Hello, {ref} has {listing(gaps)}. Please {listing(asks)} so the work can be tracked."
        )
    raise ValueError(f"no template for decision {decision!r}")


def _owner_situation(facts: Mapping[str, Any]) -> str:
    if facts["status"] == "Blocked":
        return "is blocked. Please reply with what is blocking it and who can clear the blocker."
    if facts["days_overdue"]:
        return (
            f"was due on {facts['due_date']} and is {days(facts['days_overdue'])} overdue. "
            "Please reply with the current status and a realistic completion date."
        )
    if facts["status"] == "Not Started":
        return (
            f"is due on {facts['due_date']} and has not started yet. "
            "Please confirm it is still on track."
        )
    return f"has had no update since {facts['last_update']}. Please share a short status update."


def _lateness(facts: Mapping[str, Any]) -> str:
    if facts["days_overdue"]:
        return f" and is {days(facts['days_overdue'])} past its due date of {facts['due_date']}"
    if facts["due_in_days"] == 0:
        return f" and is due today ({facts['due_date']})"
    return ""


def _gaps(facts: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    gaps, asks = [], []
    if not facts["owner"]:
        gaps.append("no owner")
        asks.append("assign an owner")
    if not facts["due_date"]:
        gaps.append("no due date")
        asks.append("set a due date")
    if facts["status"] == "Unknown":
        raw = facts["status_raw"]
        gaps.append(f'an unrecognised status ("{raw}")' if raw else "no status")
        asks.append("confirm its status")
    return gaps, asks


def _reference(facts: Mapping[str, Any]) -> str:
    ref = facts["task_id"]
    if facts["task"]:
        ref += f' "{facts["task"]}"'
    if facts["project"]:
        ref += f" ({facts['project']})"
    return ref
