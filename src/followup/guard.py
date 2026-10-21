"""Checks around what goes into the model and what comes out of it.

``screen_note``  flags notes that try to instruct an automated system. A flagged note never
                 reaches the model, and its task is decided on structured fields alone.
``check_message`` rejects drafts that bring in facts the task data does not contain, talk to
                 the wrong person or claim status changes. A rejected draft is retried once
                 and then replaced by a template.

Neither is the main defence against prompt injection: the service has no operation that
changes a task, so no text, whoever wrote it, can make it mark anything complete.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

INJECTION_FLAG = "prompt_injection_suspected"

_INSTRUCTIONS = {
    "override": r"\b(ignore|disregard|forget|override)\b.{0,40}"
    r"\b(instructions?|prompts?|rules?|above|previous)\b",
    "bulk_change": r"\b(mark|set|change|update|close)\b.{0,30}"
    r"\b(all|every|each)\b.{0,20}\btasks?\b",
    "role_play": r"\b(you are now|act as|pretend to be|system prompt|developer mode)\b",
    "fake_turn": r"(^|\n)\s*(system|assistant|developer)\s*:|</?\s*(system|instructions?)\s*>",
}
_INSTRUCTION_PATTERNS = {
    name: re.compile(pattern, re.IGNORECASE) for name, pattern in _INSTRUCTIONS.items()
}

_TASK_ID = re.compile(r"\b[A-Z]{2,5}-\d{2,6}\b")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_MONTH = (
    r"(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|"
    r"sep(t(ember)?)?|oct(ober)?|nov(ember)?|dec(ember)?)"
)
_OTHER_DATE = re.compile(
    rf"\b\d{{1,2}}/\d{{1,2}}(/\d{{2,4}})?\b"
    rf"|\b{_MONTH}\.?\s+\d{{1,2}}(st|nd|rd|th)?\b"
    rf"|\b\d{{1,2}}(st|nd|rd|th)?\s+(of\s+)?{_MONTH}\b",
    re.IGNORECASE,
)
_RELATIVE_DATE = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|tonight|"
    r"next (week|month)|end of (the )?(day|week)|eod|eow)\b",
    re.IGNORECASE,
)
_STATUS_CLAIM = re.compile(
    r"\b(has|have|had) been (marked )?(as )?(complete|completed|done|closed|finished|resolved)\b"
    r"|\b(is|are) now (marked )?(as )?(complete|completed|done|closed|finished|resolved)\b"
    r"|\bmark(ed|ing)?\b[^.]{0,30}\b(complete|completed|done|closed)\b",
    re.IGNORECASE,
)
_LINK = re.compile(r"https?://|www\.|\b[\w.+-]+@[\w-]+\.[\w.]+\b", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\{[^}]*\}|\[[^\]]*\]|<[^>]*>")
_GREETING = re.compile(
    r"^\s*(hi|hello|hey|dear|good (morning|afternoon|evening))\b[ \t]*([^,!:\n]*)",
    re.IGNORECASE,
)
_ROLE_GREETINGS = {"team", "all", "there", "everyone"}

MIN_MESSAGE, MAX_MESSAGE = 20, 700


def screen_note(note: str) -> list[str]:
    """Names of the instruction patterns found in a note; empty when the note looks like data."""
    return [name for name, pattern in _INSTRUCTION_PATTERNS.items() if pattern.search(note)]


@dataclass(frozen=True)
class MessageFacts:
    task_id: str
    recipient: str
    recipient_kind: str  # "owner" or "role"
    allowed_dates: frozenset[str]
    other_people: frozenset[str]  # owners of other tasks in the batch


def check_message(message: str, facts: MessageFacts) -> list[str]:
    """Reasons to reject a drafted message; an empty list means it may be sent."""
    problems: list[str] = []
    if not MIN_MESSAGE <= len(message) <= MAX_MESSAGE:
        problems.append(f"length {len(message)} is outside {MIN_MESSAGE}-{MAX_MESSAGE} characters")
    if facts.task_id not in message:
        problems.append(f"does not mention {facts.task_id}")
    for task_id in sorted(set(_TASK_ID.findall(message)) - {facts.task_id}):
        problems.append(f"mentions another task, {task_id}")
    for day in sorted(set(_ISO_DATE.findall(message)) - facts.allowed_dates):
        problems.append(f"date {day} is not in the task data")
    if _OTHER_DATE.search(message):
        problems.append("dates must be written as YYYY-MM-DD")
    if _RELATIVE_DATE.search(message):
        problems.append("uses a weekday or relative date the task data does not contain")
    if _STATUS_CLAIM.search(message):
        problems.append("claims or requests a status change")
    if _LINK.search(message):
        problems.append("contains a link or an e-mail address")
    if _PLACEHOLDER.search(message):
        problems.append("contains an unfilled placeholder")
    greeting = _GREETING.match(message)
    if greeting and not _addresses_recipient(greeting.group(3), facts):
        problems.append(f"greets {greeting.group(3).strip()!r} instead of the recipient")
    lowered = message.lower()
    for person in sorted(facts.other_people):
        if person.lower() in lowered:
            problems.append(f"mentions {person}, who is not on this task")
    return problems


def _addresses_recipient(name: str, facts: MessageFacts) -> bool:
    words = name.strip().lower().split()
    if set(words) <= _ROLE_GREETINGS:
        return True
    if facts.recipient_kind == "owner":
        full = facts.recipient.lower().split()
        return words in (full, full[:1])
    return set(words) <= set(facts.recipient.lower().replace("_", " ").split()) | _ROLE_GREETINGS
