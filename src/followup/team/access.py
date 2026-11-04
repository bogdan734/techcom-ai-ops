"""Who may read and write which table, enforced by SQLite's authorizer and ``mode=ro``.

The rules are table-level and column-level; which rows a person sees (their own threads, their
reports' grades) is the caller's query. SQLite has no users, so this separates code paths and
mistakes, not a hostile process that can open the file itself; the README says so.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from .org import SUPPORT_DEPARTMENT, UNROUTED, Capability, Org, Person
from .schema import CONTROL

CHAT_TABLES = frozenset({"threads", "thread_members", "chat_messages"})
_STAFF_READ = frozenset({"task_history", "quizzes", "answers"}) | CHAT_TABLES
_STAFF_WRITE = frozenset({"answers"}) | CHAT_TABLES
_ISSUER_READ = frozenset({"grades", "reviews"})
_ISSUER_WRITE = frozenset({"reviews"}) | CHAT_TABLES
_CONTROL_READ = frozenset({"task_routes", "run_ledger", "model_events"})
# The one column that would give a trust level away to someone who may not see it.
_LEVEL = ("quizzes", "level")

_NEVER = frozenset(
    value
    for name, value in vars(sqlite3).items()
    if name.startswith(("SQLITE_CREATE_", "SQLITE_DROP_"))
) | {
    sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_REINDEX,
    sqlite3.SQLITE_ANALYZE,
    sqlite3.SQLITE_ATTACH,
    sqlite3.SQLITE_DETACH,
    # Connections set their pragmas before the authorizer goes on; nobody needs one after.
    sqlite3.SQLITE_PRAGMA,
}
_HARMLESS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_SAVEPOINT,
    }
)


@dataclass(frozen=True)
class Grant:
    """``None`` for read or write means every table: the service itself."""

    read: frozenset[str] | None
    write: frozenset[str] | None
    hidden: frozenset[tuple[str, str]] = frozenset()

    @property
    def read_only(self) -> bool:
        return self.write is not None and not self.write


SERVICE = Grant(read=None, write=None)


def grant(
    viewer: Person,
    name: str,
    org: Org,
    visibility: str,
    writes: frozenset[str] = frozenset(),
) -> Grant:
    """The grant for ``viewer`` on database ``name``; PermissionError when there is none."""
    issuer = org.can(viewer.id, Capability.ISSUE_TASKS)
    sees_trust = org.can(viewer.id, Capability.SEE_TRUST) and visibility == "issuers"
    if name == SUPPORT_DEPARTMENT:
        readable, writable = CHAT_TABLES, CHAT_TABLES
    elif name in (CONTROL, UNROUTED):
        if not issuer:
            raise PermissionError(f"{viewer.id} may not open {name}: managers and up only")
        if name == CONTROL:
            readable = _CONTROL_READ | ({"trust_events"} if sees_trust else set())
            writable = frozenset()
        else:
            readable, writable = _STAFF_READ | _ISSUER_READ, _ISSUER_WRITE
    else:
        own = viewer.department == name
        if not (own or issuer):
            raise PermissionError(f"{viewer.id} may not open the {name} department")
        readable = _STAFF_READ | (_ISSUER_READ if issuer else frozenset())
        writable = (_STAFF_WRITE if own else CHAT_TABLES) | (_ISSUER_WRITE if issuer else set())
    refused = writes - writable
    if refused:
        raise PermissionError(f"{viewer.id} may not write {sorted(refused)} in {name}")
    return Grant(frozenset(readable), frozenset(writes), frozenset() if sees_trust else {_LEVEL})


def authorizer(given: Grant) -> Callable[..., int]:
    """An ``sqlite3`` authorizer: ATTACH, DDL and PRAGMA for nobody, tables and columns by grant."""

    def decide(action: int, arg1: str | None, arg2: str | None, *_: object) -> int:
        if action in _NEVER:
            return sqlite3.SQLITE_DENY
        if given.read is None:
            return sqlite3.SQLITE_OK
        if action in _HARMLESS:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            if arg1 not in given.read:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_IGNORE if (arg1, arg2) in given.hidden else sqlite3.SQLITE_OK
        writes = (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE)
        if action in writes and given.write is not None and arg1 in given.write:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return decide
