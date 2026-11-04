"""Threads between the person doing a task and the person who issued it, and support tickets.

A thread belongs to a task (or is a support ticket) and has members with a role. A question
sits unanswered until a human on the other side replies or the assistant quotes the spec; an
unanswered question climbs the reporting line one level per SLA, as in a paging policy. Posting
the same text twice is one message, and nothing here sends: the notices it builds go through the
outbox.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal

from followup.guard import screen_text

from .notices import Channel, Kind, Message
from .org import Org, Person, Role
from .settings import TeamSettings
from .spec_terms import normalise_ws

ASSISTANT = "assistant"
_ASKERS = frozenset({"assignee", "asker"})
ThreadKind = Literal["task", "support"]


@dataclass(frozen=True)
class Post:
    message_id: str
    thread_id: str
    author: str
    text: str
    day: date
    flagged: bool
    quote: str | None = None  # set on an assistant reply that quoted the spec


@dataclass(frozen=True)
class Pending:
    """A question nobody has answered yet."""

    thread_id: str
    kind: ThreadKind
    task_id: str
    question: Post
    members: Mapping[str, str]


def thread_id(kind: ThreadKind, ref: str) -> str:
    return hashlib.sha256(f"{kind}|{ref}".encode()).hexdigest()[:16]


def open_thread(
    db: sqlite3.Connection,
    *,
    kind: ThreadKind,
    ref: str,
    subject: str,
    opener: str,
    members: Mapping[str, str],
    day: date,
) -> str:
    """Create the thread if it is new; a second call with the same ref changes nothing."""
    tid = thread_id(kind, ref)
    db.execute(
        "INSERT OR IGNORE INTO threads (thread_id, kind, task_id, opener_id, subject, opened_on) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tid, kind, ref if kind == "task" else None, opener, subject, day.isoformat()),
    )
    for person, role in {**members, ASSISTANT: "assistant"}.items():
        db.execute(
            "INSERT OR IGNORE INTO thread_members (thread_id, person_id, role) VALUES (?, ?, ?)",
            (tid, person, role),
        )
    return tid


def members_of(db: sqlite3.Connection, thread: str) -> dict[str, str]:
    rows = db.execute(
        "SELECT person_id, role FROM thread_members WHERE thread_id = ? ORDER BY person_id",
        (thread,),
    )
    return dict(rows.fetchall())


def post(
    db: sqlite3.Connection,
    thread: str,
    author: str,
    text: str,
    day: date,
    quote: str | None = None,
) -> Post:
    """Add a message; the id comes from thread, author and text, so repeating it adds nothing."""
    if author not in members_of(db, thread):
        raise PermissionError(f"{author} is not in thread {thread}")
    clean = normalise_ws(text)
    mid = hashlib.sha256(f"{thread}|{author}|{clean}".encode()).hexdigest()[:32]
    flagged = bool(screen_text(clean))
    db.execute(
        "INSERT OR IGNORE INTO chat_messages "
        "(message_id, thread_id, author_id, body, quote, flagged, posted_on) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (mid, thread, author, clean, quote, int(flagged), day.isoformat()),
    )
    return next(p for p in _posts(db, thread) if p.message_id == mid)


def read_thread(db: sqlite3.Connection, thread: str, viewer: str) -> list[Post]:
    """The row filter: only members read a thread."""
    if viewer not in members_of(db, thread):
        raise PermissionError(f"{viewer} is not in thread {thread}")
    return _posts(db, thread)


def pending_questions(db: sqlite3.Connection) -> list[Pending]:
    """Threads whose latest question has no human answer and no quoted reply from the assistant."""
    found = []
    for tid, kind, task_id, _ in _threads(db):
        members = members_of(db, tid)
        waiting = _waiting(_posts(db, tid), members)
        if waiting is not None:
            found.append(Pending(tid, kind, task_id, waiting, members))
    return found


def climb(
    org: Org, issuer_id: str, asked_on: date, today: date, sla_days: int
) -> tuple[Person, ...]:
    """Who the question has reached beyond the issuer: one level up per full SLA, up to the boss."""
    steps = max((today - asked_on).days, 0) // sla_days
    return org.chain_above(issuer_id)[:steps]


def chat_notices(
    db: sqlite3.Connection, org: Org, today: date, team: TeamSettings
) -> list[Message]:
    """New posts to the other side, and escalations of questions that waited past the SLA."""
    messages: list[Message] = []
    support = [p.id for p in org.with_role(Role.SUPPORT)]
    for tid, kind, task_id, _ in _threads(db):
        members = members_of(db, tid)
        posts = _posts(db, tid)
        ref = task_id if kind == "task" else f"SUPPORT-{tid[:8]}"
        for index, current in enumerate(posts):
            messages += _post_notices(kind, ref, current, posts[index + 1 :], members, org)
        waiting = _waiting(posts, members)
        if waiting is None:
            continue
        sla = team.support_sla_days if kind == "support" else team.chat_sla_days
        start = _issuer(members) if kind == "task" else (support[0] if support else None)
        if start is None:
            continue
        for rung, person in enumerate(climb(org, start, waiting.day, today, sla), start=1):
            facts = {
                "author": _name(org, waiting.author),
                "task_id": ref,
                "days": str((today - waiting.day).days),
                "text": waiting.text,
            }
            state = f"{waiting.message_id}|{rung}"
            messages.append(
                Message(Kind.CHAT_ESCALATED, ref, person.id, Channel.MESSAGE, state, facts)
            )
    return messages


# -- internals -----------------------------------------------------------------------------


def _threads(db: sqlite3.Connection) -> list[tuple[str, ThreadKind, str, str]]:
    rows = db.execute(
        "SELECT thread_id, kind, COALESCE(task_id, ''), opener_id FROM threads ORDER BY thread_id"
    )
    return list(rows.fetchall())


def _posts(db: sqlite3.Connection, thread: str) -> list[Post]:
    rows = db.execute(
        "SELECT message_id, thread_id, author_id, body, posted_on, flagged, quote "
        "FROM chat_messages WHERE thread_id = ? ORDER BY posted_on, rowid",
        (thread,),
    ).fetchall()
    return [Post(m, t, a, b, date.fromisoformat(d), bool(f), q) for m, t, a, b, d, f, q in rows]


def _waiting(posts: list[Post], members: Mapping[str, str]) -> Post | None:
    """The oldest question since the last answer; ``None`` when nothing is waiting."""
    waiting: Post | None = None
    for current in posts:
        role = members.get(current.author, "")
        if role in _ASKERS:
            waiting = waiting or current
        elif role == "assistant":
            if current.quote is not None:
                waiting = None  # a quoted reply settles the question until the asker writes again
        else:
            waiting = None  # a human on the answering side replied
    return waiting


def _issuer(members: Mapping[str, str]) -> str | None:
    return next((person for person, role in sorted(members.items()) if role == "issuer"), None)


def _name(org: Org, author: str) -> str:
    return "Assistant" if author == ASSISTANT else org.person(author).name


def _post_notices(
    kind: ThreadKind,
    ref: str,
    current: Post,
    later: list[Post],
    members: Mapping[str, str],
    org: Org,
) -> list[Message]:
    role = members.get(current.author, "")
    humans = {p: r for p, r in members.items() if r != "assistant" and p != current.author}
    if role in _ASKERS:
        # A question the assistant answered with a quote does not bother the issuer.
        reply = next((p for p in later if members.get(p.author) != role), None)
        if reply is not None and members.get(reply.author) == "assistant" and reply.quote:
            return []
        recipients = [p for p, r in humans.items() if r not in _ASKERS]
    else:
        recipients = [p for p, r in humans.items() if r in _ASKERS]
    first_of_ticket = kind == "support" and role in _ASKERS
    notice = Kind.SUPPORT_TICKET if first_of_ticket else Kind.CHAT_QUESTION
    facts = {"author": _name(org, current.author), "task_id": ref, "text": current.text}
    return [
        Message(notice, ref, person, Channel.MESSAGE, current.message_id, facts)
        for person in sorted(recipients)
    ]
