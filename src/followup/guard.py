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
_QUANTITY = re.compile(r"\b(\d+)\s*-?\s*(day|hour|hr|minute|min|week|month|year)s?\b", re.I)
_WORD = re.compile(r"[A-Za-z]+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
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
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|tonight|yesterday|"
    r"noon|midday|midnight|next (week|month)|this (morning|afternoon|evening|week|month)|"
    r"end of (the )?(day|week|month)|(close|end) of business|eod|eow|cob|eob)\b"
    r"|\b\d{1,2}(:\d{2})?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])",
    re.IGNORECASE,
)
_SPELLED_DURATION = re.compile(
    r"\b(a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|"
    r"twenty|thirty|forty|forty-eight|seventy-two|several|a couple of|a few|half an?|another)"
    r"\s+(more\s+)?((business|working)\s+)?(hours?|hrs?|minutes?|days?|weeks?|months?)\b"
    r"|\bfortnight\b",
    re.IGNORECASE,
)
_STATE = (
    r"(complete|completed|done|closed|finished|resolved|blocked|not started|in progress|"
    r"working on it|on hold|cancell?ed)"
)
_STATUS_CHANGE = re.compile(
    rf"\b(has|have|had) been (marked )?(as )?{_STATE}\b"
    rf"|\b(is|are) now (marked )?(as )?{_STATE}\b"
    rf"|\bmark(s|ed|ing)?\b[^.]{{0,30}}\b{_STATE}\b"
    rf"|\bstatus\b[^.]{{0,20}}\b(to|as)\b\W{{0,2}}{_STATE}\b"
    rf"|\b(set|change|switch|move|update)\b[^.]{{0,30}}\b(to|as)\b\W{{0,2}}{_STATE}\b"
    r"|\bclos(e|es|ed|ing)\s+(it|this|that|out|the task|this task|the ticket)\b"
    r"|\bcancel(s|l?ed|l?ing|lation)?\b"
    r"|\bhand(s|ed|ing)?\b[^.]{0,20}\b(over|off|to)\b"
    r"|\btak(e|es|en|ing)\b[^.]{0,15}\bover\b"
    r"|\bre-?assign(s|ed|ing|ment)?\b",
    re.IGNORECASE,
)
_LINK = re.compile(r"https?://|www\.|\b[\w.+-]+@[\w-]+\.[\w.]+\b", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\{[^}]*\}|\[[^\]]*\]|<[^>]*>")
_GREETING = re.compile(
    r"^\s*(hi|hello|hey|dear|good (morning|afternoon|evening))\b[ \t]*([^,!:\n]*)",
    re.IGNORECASE,
)
_ROLE_GREETINGS = {"team", "all", "there", "everyone"}
# Capitalised words and acronyms a message may use without the task data naming them.
_COMMON_TEXT = """
    hi hello hey dear good morning afternoon evening please could can would will may might
    should kindly thanks thank regards best kind cheers the this that these those it its we
    our us you your they their there here if once when as so also and but or for to let just
    note quick reminder regarding re hope sorry any no yes what who which how why where given
    based due since because following per after before until while currently update status
    task project owner team all everyone escalating escalation overdue blocked done not
    started working on in at by of with from about materials material delivery manager
    warehouse coordinator operations procurement site field crew
"""
_COMMON_WORDS = frozenset(_COMMON_TEXT.split())
_ACRONYMS = frozenset({"eta", "pm", "id", "ok", "fyi", "asap", "utc"})

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
    phrases: tuple[str, ...]  # task fields a message may quote verbatim (title, owner, ...)
    allowed_dates: frozenset[str]
    allowed_numbers: frozenset[str]
    allowed_quantities: frozenset[tuple[str, str]]  # ("3", "day")
    known_words: frozenset[str]  # lower-case words of the task data
    other_people: frozenset[str]  # owners of other tasks in the batch

    @classmethod
    def grounded_in(
        cls,
        task_id: str,
        recipient: str,
        recipient_kind: str,
        phrases: list[str],
        context: list[str],
        dates: list[str],
        days: list[int],
        other_people: frozenset[str],
    ) -> MessageFacts:
        """Limits from the task data.

        ``phrases`` are fields a message may quote as they are; ``context`` is the rest of the
        data (reason, note) whose words, numbers and quantities a message may repeat; ``days``
        are the day counts the rules computed.
        """
        text = " ".join([task_id, *phrases, *context])
        return cls(
            task_id=task_id,
            recipient=recipient,
            recipient_kind=recipient_kind,
            phrases=tuple(sorted({p for p in phrases if p}, key=len, reverse=True)),
            allowed_dates=frozenset(dates),
            allowed_numbers=frozenset(
                [*_NUMBER.findall(_ISO_DATE.sub(" ", text)), *(str(n) for n in days if n >= 0)]
            ),
            allowed_quantities=frozenset(
                [*_quantities(text), *((str(n), "day") for n in days if n >= 0)]
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
    greeting = _GREETING.match(message)
    if greeting and not _addresses_recipient(greeting.group(3), facts):
        problems.append(f"greets {greeting.group(3).strip()!r} instead of the recipient")

    # Quoting the task's own title, project or owner is grounded by definition, whatever
    # those fields contain; everything else in the message is checked against the facts.
    rest = message
    for phrase in facts.phrases:
        rest = rest.replace(phrase, " ")
    for task_id in sorted(set(_TASK_ID.findall(rest)) - {facts.task_id}):
        problems.append(f"mentions another task, {task_id}")
    for day in sorted(set(_ISO_DATE.findall(rest)) - facts.allowed_dates):
        problems.append(f"date {day} is not in the task data")
    if _OTHER_DATE.search(rest):
        problems.append("dates must be written as YYYY-MM-DD")
    if _RELATIVE_DATE.search(rest):
        problems.append(
            "uses a weekday, time of day or relative date the task data does not contain"
        )
    digits = _ISO_DATE.sub(" ", rest.replace(facts.task_id, " "))
    for number in sorted(set(_NUMBER.findall(digits)) - facts.allowed_numbers, key=_numeric):
        problems.append(f"number {number} is not in the task data")
    for count, unit in sorted(_quantities(digits) - facts.allowed_quantities):
        problems.append(f"'{count} {unit}s' is not in the task data")
    if _SPELLED_DURATION.search(rest):
        problems.append("states a duration the task data does not contain")
    if _STATUS_CHANGE.search(rest):
        problems.append("claims, requests or suggests a status change, hand-over or reassignment")
    if _LINK.search(rest):
        problems.append("contains a link or an e-mail address")
    if _PLACEHOLDER.search(rest):
        problems.append("contains an unfilled placeholder")
    lowered = rest.lower()
    for person in sorted(facts.other_people):
        if person.lower() in lowered:
            problems.append(f"mentions {person}, who is not on this task")
    unknown = _unknown_names(rest, facts)
    if unknown:
        problems.append(f"names {', '.join(unknown)}, which the task data does not mention")
    return problems


def _quantities(text: str) -> set[tuple[str, str]]:
    units = {"hr": "hour", "min": "minute"}
    return {
        (count, units.get(unit.lower(), unit.lower())) for count, unit in _QUANTITY.findall(text)
    }


def _unknown_names(text: str, facts: MessageFacts) -> list[str]:
    """Names and acronyms that neither the task data nor plain English explains.

    A capital letter at the start of a sentence is grammar, not a name, so those words are
    skipped; acronyms are checked wherever they stand.
    """
    names = set()
    for sentence in _SENTENCE.split(text):
        for position, word in enumerate(_WORD.findall(sentence)):
            lowered = word.lower()
            if len(word) < 2 or not word[0].isupper() or lowered in facts.known_words:
                continue
            if word.isupper():
                if lowered not in _ACRONYMS:
                    names.add(word)
            elif position > 0 and lowered not in _COMMON_WORDS:
                names.add(word)
    return sorted(names)


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
