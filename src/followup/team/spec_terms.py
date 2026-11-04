"""What a task spec says, found by rules: requirements, limits and identifiers.

These are the floor under the model. A model-written question or must-have command counts only
if its quote appears in the spec (``contains_verbatim``); the rules here also give a quiz and a
code trace when no model is available.
"""

from __future__ import annotations

import hashlib
import re

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_REQUIREMENT = re.compile(
    r"\b(?:must|shall|should|needs? to|has to|have to|is required to|are required to)\b",
    re.IGNORECASE,
)
_NUMBER = r"\d+(?:[.,]\d+)?"
_LIMIT = re.compile(
    rf"\b(?:(?:at least|at most|no more than|no less than|up to|within|under|over|exactly)\s+)?"
    rf"{_NUMBER}\s?(?:%|[A-Za-z]+(?:/[A-Za-z]+)?)(?![A-Za-z])",
    re.IGNORECASE,
)
_NOT_UNITS = frozenset(
    re.findall(
        r"[a-z]+",
        """and or of to the a an in on at as is are be by for with from that this it its if then
        january february march april may june july august september october november december""",
    )
)
_IDENTIFIER_PATTERNS = (
    re.compile(r"`([^`\n]{1,80})`"),
    re.compile(r"[\"“]([^\"”\n]{1,40})[\"”]"),
    re.compile(r"(?<![\w-])(--[a-z][\w-]*)"),
    re.compile(r"(?<![\w/])(/[A-Za-z][\w./{}-]*)"),
    re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b"),
    re.compile(r"\b([a-z][a-z0-9]+(?:\.[a-z][a-z0-9]+)+)\b"),
    re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b"),
)
_WORD = re.compile(rf"{_NUMBER}|[A-Za-z][A-Za-z0-9_-]*")
_STOPWORDS = frozenset(
    re.findall(
        r"[a-z]+",
        """the a an and or of to in on for with by is are be must shall should need needs at as
        that this it its from all any each when if then than not no can may will have has was
        were""",
    )
)


def normalise_ws(text: str) -> str:
    return " ".join(text.split())


def contains_verbatim(source: str, quote: str) -> bool:
    """Case-sensitive match after collapsing whitespace; an empty quote proves nothing."""
    needle = normalise_ws(quote)
    return bool(needle) and needle in normalise_ws(source)


def spec_hash(spec: str) -> str:
    return hashlib.sha256(normalise_ws(spec).encode("utf-8")).hexdigest()[:32]


def sentences(spec: str) -> list[str]:
    parts = (_BULLET.sub("", part) for part in _SENTENCE_BREAK.split(spec))
    return [clean for part in parts if (clean := normalise_ws(part))]


def requirement_sentences(spec: str) -> list[str]:
    return [sentence for sentence in sentences(spec) if _REQUIREMENT.search(sentence)]


def limits(spec: str) -> list[str]:
    """Numbers with a unit or a comparator, such as ``0.5 dB`` or ``within 48 hours``."""
    found = []
    for match in _LIMIT.finditer(normalise_ws(spec)):
        text = match.group(0)
        if text.split()[-1].lower() not in _NOT_UNITS:
            found.append(text)
    return list(dict.fromkeys(found))


def identifiers(spec: str) -> list[str]:
    """Commands, flags, routes and names in order of appearance, each once."""
    text = normalise_ws(spec)
    hits = [
        (match.start(1), match.group(1).strip())
        for pattern in _IDENTIFIER_PATTERNS
        for match in pattern.finditer(text)
    ]
    ordered = (name for _, name in sorted(hits) if name)
    return list(dict.fromkeys(ordered))


def key_terms(quote: str, limit: int = 4) -> tuple[str, ...]:
    """The words an answer to a question about ``quote`` has to contain.

    Taken from the quote and nowhere else, so a term can never be something the spec did not
    say. Numbers and long words win; the result keeps the order of the quote.
    """
    words = [(index, word.lower()) for index, word in enumerate(_WORD.findall(quote))]
    candidates = {}
    for index, word in words:
        if word in _STOPWORDS or (len(word) < 3 and not word[0].isdigit()):
            continue
        candidates.setdefault(word, index)
    ranked = sorted(
        candidates, key=lambda word: (not word[0].isdigit(), -len(word), candidates[word])
    )
    return tuple(sorted(ranked[:limit], key=candidates.__getitem__))
