"""Checks around what goes into the model and what comes out of it.

``screen_text``   flags free text (notes, titles) that tries to instruct an automated system.
                  A flagged task is handled without the model, on its structured fields.
``check_message`` rejects drafts that bring in anything the task data does not contain:
                  dates, numbers, people, other tasks, deadlines, status changes, links. A
                  rejected draft is retried once and then replaced by a template.

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
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_WORD = re.compile(r"[A-Za-z]+")
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
_SPELLED_DURATION = re.compile(
    r"\b(a|an|one|two|three|four|five|six|seven|eight|nine|ten|a couple of|a few)\s+"
    r"(more\s+)?(business\s+)?(hours?|days?|weeks?)\b",
    re.IGNORECASE,
)
_FINAL = r"(complete|completed|done|closed|finished|resolved)"
_STATUS_CHANGE = re.compile(
    rf"\b(has|have|had) been (marked )?(as )?{_FINAL}\b"
    rf"|\b(is|are) now (marked )?(as )?{_FINAL}\b"
    rf"|\bmark(ed|ing)?\b[^.]{{0,30}}\b{_FINAL}\b"
    r"|\bstatus\b[^.]{0,20}\b(to|as)\b\W{0,2}"
    r"(done|complete|completed|closed|finished|resolved|blocked|not started|working on it)\b"
    rf"|\b(set|change|switch|move|update)\b[^.]{{0,30}}\b(to|as)\b\W{{0,2}}{_FINAL}\b"
    r"|\bclos(e|ed|ing)\b[^.]{0,20}\b(the task|this task|the ticket|it out|out the task)\b"
    r"|\bre-?assign(ed|ing|ment)?\b",
    re.IGNORECASE,
)
_LINK = re.compile(r"https?://|www\.|\b[\w.+-]+@[\w-]+\.[\w.]+\b", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\{[^}]*\}|\[[^\]]*\]|<[^>]*>")
_GREETING = re.compile(
    r"^\s*(hi|hello|hey|dear|good (morning|afternoon|evening))\b[ \t]*([^,!:\n]*)",
    re.IGNORECASE,
)
_ROLE_GREETINGS = {"team", "all", "there", "everyone"}
# Capitalised words a message may use without the task data naming them.
_COMMON_WORDS = frozenset(
    [
        "hi",
        "hello",
        "hey",
        "dear",
        "good",
        "morning",
        "afternoon",
        "evening",
        "please",
        "could",
        "can",
        "would",
        "will",
        "may",
        "might",
        "should",
        "kindly",
        "thanks",
        "thank",
        "regards",
        "best",
        "kind",
        "cheers",
        "the",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "we",
        "our",
        "us",
        "you",
        "your",
        "they",
        "their",
        "there",
        "here",
        "if",
        "once",
        "when",
        "as",
        "so",
        "also",
        "and",
        "but",
        "or",
        "for",
        "to",
        "let",
        "just",
        "note",
        "fyi",
        "quick",
        "reminder",
        "regarding",
        "re",
        "hope",
        "sorry",
        "any",
        "no",
        "yes",
        "what",
        "who",
        "which",
        "how",
        "why",
        "where",
        "given",
        "based",
        "due",
        "since",
        "because",
        "following",
        "per",
        "after",
        "before",
        "until",
        "while",
        "currently",
        "update",
        "status",
        "task",
        "project",
        "owner",
        "team",
        "all",
        "everyone",
        "escalating",
        "escalation",
        "overdue",
        "blocked",
        "done",
        "not",
        "started",
        "working",
        "on",
        "in",
        "at",
        "by",
        "of",
        "with",
        "from",
        "about",
        "materials",
        "material",
        "delivery",
        "manager",
        "warehouse",
        "coordinator",
        "operations",
        "procurement",
        "site",
        "field",
        "crew",
        "i",
    ]
)

MIN_MESSAGE, MAX_MESSAGE = 20, 700


def screen_text(text: str) -> list[str]:
    """Names of the instruction patterns found in free text; empty when it looks like data."""
    return [name for name, pattern in _INSTRUCTION_PATTERNS.items() if pattern.search(text)]


@dataclass(frozen=True)
class MessageFacts:
    """What a message about one task may contain."""

    task_id: str
    recipient: str
    recipient_kind: str  # "owner" or "role"
    allowed_dates: frozenset[str]
    allowed_numbers: frozenset[str]
    known_words: frozenset[str]  # lower-case words of the task data
    other_people: frozenset[str]  # owners of other tasks in the batch

    @classmethod
    def grounded_in(
        cls,
        task_id: str,
        recipient: str,
        recipient_kind: str,
        texts: list[str],
        dates: list[str],
        numbers: list[int],
        other_people: frozenset[str],
    ) -> MessageFacts:
        """Limits from the task data: words, digits and dates the message may repeat."""
        text = " ".join(texts)
        return cls(
            task_id=task_id,
            recipient=recipient,
            recipient_kind=recipient_kind,
            allowed_dates=frozenset(dates),
            allowed_numbers=frozenset(
                [*_NUMBER.findall(_ISO_DATE.sub(" ", text)), *(str(n) for n in numbers if n >= 0)]
            ),
            known_words=frozenset(word.lower() for word in _WORD.findall(text)),
            other_people=other_people,
        )


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
    digits = _ISO_DATE.sub(" ", message.replace(facts.task_id, " "))
    for number in sorted(set(_NUMBER.findall(digits)) - facts.allowed_numbers, key=_numeric):
        problems.append(f"number {number} is not in the task data")
    if _SPELLED_DURATION.search(message):
        problems.append("states a duration the task data does not contain")
    if _STATUS_CHANGE.search(message):
        problems.append("claims, requests or suggests a status change or reassignment")
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
    unknown = sorted(
        {
            word
            for word in _WORD.findall(message)
            if len(word) > 1
            and word[0].isupper()
            and not word.isupper()
            and word.lower() not in facts.known_words | _COMMON_WORDS
        }
    )
    if unknown:
        problems.append(f"names {', '.join(unknown)}, which the task data does not mention")
    return problems


def _numeric(number: str) -> tuple[int, str]:
    return len(number), number


def _addresses_recipient(name: str, facts: MessageFacts) -> bool:
    words = name.strip().lower().split()
    if set(words) <= _ROLE_GREETINGS:
        return True
    if facts.recipient_kind == "owner":
        full = facts.recipient.lower().split()
        return words in (full, full[:1])
    return set(words) <= set(facts.recipient.lower().replace("_", " ").split()) | _ROLE_GREETINGS
