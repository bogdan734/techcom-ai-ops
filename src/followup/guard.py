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
from functools import lru_cache

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
_SPELLED_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40, "forty-eight": 48, "seventy-two": 72,
}  # fmt: skip
_NUMBER_WORDS = "|".join(sorted(_SPELLED_NUMBERS, key=len, reverse=True))
_UNIT = r"(?P<unit>day|hour|hr|minute|min|week|month|year)s?\b"
_SPELLED_QUANTITY = re.compile(
    rf"\b(?P<count>{_NUMBER_WORDS})\s+(more\s+)?((business|working)\s+)?{_UNIT}", re.I
)
_VAGUE_DURATION = re.compile(
    r"\b(several|a couple of|a few|few|half an?|another|many)\s+(more\s+)?"
    r"((business|working)\s+)?(hours?|hrs?|minutes?|days?|weeks?|months?)\b|\bfortnight\b",
    re.IGNORECASE,
)
_DEADLINE = re.compile(
    rf"(?<!due )\b(within|in|over the next|in the next|for the next)\s+(\d+|{_NUMBER_WORDS})\s+"
    r"(more\s+)?((business|working)\s+)?(hours?|hrs?|minutes?|mins?|days?|weeks?|months?)\b"
    rf"|\b(\d+|{_NUMBER_WORDS})\s+(hours?|days?|weeks?)\s+from\s+(now|today)\b",
    re.IGNORECASE,
)
_STATES = (
    "complete", "completed", "done", "closed", "finished", "resolved", "blocked",
    "not started", "in progress", "working on it", "on hold", "cancelled", "canceled",
)  # fmt: skip
# Saying that a task has its current status is not a status change.
_CURRENT = {
    "Blocked": {"blocked"},
    "Not Started": {"not started"},
    "Working on it": {"in progress", "working on it"},
}
_HAND_OVER = (
    r"|(?<!on )(?<!at )(?<!in )\bhand(s|ed|ing)?\s+"
    r"((it|this|that|them|the task|the work|everything)\s+)?(over|off|to)\b"
)
_OTHER_CHANGES = (
    r"|\bclos(e|es|ed|ing)\s+(it|this|that|out|the task|this task|the ticket)\b"
    r"|\bcancel(s|l?ed|l?ing|lation)?\b"
    r"|\btak(e|es|en|ing)\b[^.]{0,15}\bover\b"
    r"|\bre-?assign(s|ed|ing|ment)?\b"
)


@lru_cache(maxsize=8)
def _status_change(current: str) -> re.Pattern[str]:
    states = "(" + "|".join(s for s in _STATES if s not in _CURRENT.get(current, set())) + ")"
    return re.compile(
        rf"\b(has|have|had) been (marked )?(as )?{states}\b"
        rf"|\b(is|are) now (marked )?(as )?{states}\b"
        rf"|\bmark(s|ed|ing)?\b[^.]{{0,30}}\b{states}\b"
        rf"|\bstatus\b[^.]{{0,20}}\b(to|as)\b\W{{0,2}}{states}\b"
        rf"|\b(set|change|switch|move|update)\b[^.]{{0,30}}\b(to|as)\b\W{{0,2}}{states}\b"
        + _HAND_OVER
        + _OTHER_CHANGES,
        re.IGNORECASE,
    )


_LINK = re.compile(r"https?://|www\.|\b[\w.+-]+@[\w-]+\.[\w.]+\b", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\{[^}]*\}|\[[^\]]*\]|<[^>]*>")
_GREETING = re.compile(
    r"^\s*(hi|hello|hey|dear|good (morning|afternoon|evening))\b[ \t]*([^,!:\n]*)",
    re.IGNORECASE,
)
_ROLE_GREETINGS = {"team", "all", "there", "everyone"}
# Capitalised words a message may use without the task data naming them: greetings,
# sentence openers and the vocabulary of a follow-up. Anything else must come from the data.
_COMMON_TEXT = """
    hi hello hey dear good morning afternoon evening please kindly thanks thank regards best
    kind cheers sorry apologies hope reply confirm share send let could would can will may
    might must should shall do does did is are was were has have had the a an this that these
    those it its we our us me my you your they their he she his her there here if once when
    while as so also and but or for to of in on at by with from about without until since
    given because although though even still only both each every some any more other another
    such same again soon already ideally currently now today then however meanwhile otherwise
    unfortunately note just quick reminder following regarding re per what who which how why
    where no yes appreciate appreciated grateful ensure make sure check see call contact
    coordinate plan schedule reschedule provide give tell advise inform flag raise escalate
    escalating escalation follow looking checking chasing waiting awaiting urgent important
    action required needed request requesting asked asking update status task tasks project
    owner team all everyone overdue blocked done not started working materials material
    delivery deliveries manager warehouse coordinator operations procurement site field crew
    finish complete review help due ask keep get try use start proceed continue resume
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
    status: str  # the task's current status, which a message may state
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
        status: str,
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
            status=status,
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
    if _VAGUE_DURATION.search(rest):
        problems.append("states a duration the task data does not contain")
    if _DEADLINE.search(rest):
        problems.append("sets a deadline the task data does not contain")
    if _status_change(facts.status).search(rest):
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
    """("3", "day") for "3 days", "3-day" or "three days"."""
    units = {"hr": "hour", "min": "minute"}
    found = {(count, unit.lower()) for count, unit in _QUANTITY.findall(text)}
    for match in _SPELLED_QUANTITY.finditer(text):
        found.add((str(_SPELLED_NUMBERS[match["count"].lower()]), match["unit"].lower()))
    return {(count, units.get(unit, unit)) for count, unit in found}


def _unknown_names(text: str, facts: MessageFacts) -> list[str]:
    """Capitalised words and acronyms that neither the task data nor plain English explains."""
    names = set()
    for word in _WORD.findall(text):
        lowered = word.lower()
        if len(word) < 2 or not word[0].isupper() or lowered in facts.known_words:
            continue
        if lowered not in (_ACRONYMS if word.isupper() else _COMMON_WORDS):
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
