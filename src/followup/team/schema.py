"""The team tables, as ordered migration steps with checksums.

A department file holds the base outbox tables (``Store``) plus the tables here. The control
file holds what belongs to a person or a run and not to one department. A step is never edited
after it has been applied somewhere: the ledger stores its checksum and a changed step is an
error, so a new change is a new step.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# "FOLW" in ASCII. A SQLite file of another program, or the old single state.db, reads as 0.
APPLICATION_ID = 0x464F4C57
CONTROL = "_control"

BOOTSTRAP = f"""
PRAGMA application_id = {APPLICATION_ID};
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE team_migrations (
    seq    INTEGER PRIMARY KEY,
    name   TEXT NOT NULL,
    sha256 TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Step:
    name: str
    sql: str

    @property
    def sha256(self) -> str:
        # Whitespace-normalised, so re-indenting the SQL is not a change to the schema.
        text = f"{self.name}\n{' '.join(self.sql.split())}"
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


_DEPARTMENT_TABLES = Step(
    "001-tables",
    """
    CREATE TABLE task_history (
        task_id       TEXT NOT NULL,
        field         TEXT NOT NULL CHECK (field IN ('spec', 'status')),
        value         TEXT NOT NULL,
        detail        TEXT NOT NULL DEFAULT '',
        first_seen    TEXT NOT NULL,
        on_activation INTEGER NOT NULL,
        PRIMARY KEY (task_id, field, value)
    );
    CREATE TABLE pins (
        key     TEXT PRIMARY KEY,
        kind    TEXT NOT NULL,
        payload TEXT NOT NULL,
        model   TEXT NOT NULL,
        day     TEXT NOT NULL
    );
    CREATE TABLE quizzes (
        quiz_id   TEXT PRIMARY KEY,
        task_id   TEXT NOT NULL,
        person_id TEXT NOT NULL,
        issuer_id TEXT,
        spec_hash TEXT NOT NULL,
        level     TEXT NOT NULL,
        questions TEXT NOT NULL,
        issued_on TEXT NOT NULL
    );
    CREATE TABLE answers (
        quiz_id     TEXT NOT NULL,
        question_id TEXT NOT NULL,
        answer      TEXT NOT NULL,
        flagged     INTEGER NOT NULL DEFAULT 0,
        answered_on TEXT NOT NULL,
        PRIMARY KEY (quiz_id, question_id)
    );
    CREATE TABLE grades (
        quiz_id   TEXT PRIMARY KEY,
        score     REAL NOT NULL,
        passed    INTEGER NOT NULL,
        status    TEXT NOT NULL CHECK (status IN ('graded', 'needs_review')),
        detail    TEXT NOT NULL,
        graded_on TEXT NOT NULL
    );
    CREATE TABLE reviews (
        quiz_id     TEXT PRIMARY KEY,
        reviewer_id TEXT NOT NULL,
        accepted    INTEGER NOT NULL,
        note        TEXT NOT NULL,
        reviewed_on TEXT NOT NULL
    );
    CREATE TABLE threads (
        thread_id TEXT PRIMARY KEY,
        kind      TEXT NOT NULL CHECK (kind IN ('task', 'support')),
        task_id   TEXT,
        opener_id TEXT NOT NULL,
        subject   TEXT NOT NULL,
        status    TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered', 'closed')),
        rung      INTEGER NOT NULL DEFAULT 0,
        opened_on TEXT NOT NULL
    );
    CREATE TABLE thread_members (
        thread_id TEXT NOT NULL,
        person_id TEXT NOT NULL,
        role      TEXT NOT NULL CHECK (
            role IN ('assignee', 'issuer', 'asker', 'support', 'assistant')
        ),
        PRIMARY KEY (thread_id, person_id)
    );
    CREATE TABLE chat_messages (
        message_id TEXT PRIMARY KEY,
        thread_id  TEXT NOT NULL,
        author_id  TEXT NOT NULL,
        body       TEXT NOT NULL,
        quote      TEXT,
        flagged    INTEGER NOT NULL DEFAULT 0,
        posted_on  TEXT NOT NULL
    );
    CREATE TABLE call_results (
        call_key    TEXT PRIMARY KEY,
        person_id   TEXT NOT NULL,
        answered_by TEXT NOT NULL CHECK (answered_by IN ('person', 'machine', 'none')),
        purpose     TEXT NOT NULL CHECK (purpose IN ('reminder', 'verification')),
        placed_on   TEXT NOT NULL
    );
    """,
)
_DEPARTMENT_INDEXES = Step(
    "002-indexes",
    """
    CREATE INDEX quizzes_task ON quizzes (task_id);
    CREATE INDEX chat_thread ON chat_messages (thread_id, posted_on);
    CREATE INDEX threads_task ON threads (task_id);
    """,
)
DEPARTMENT_STEPS: tuple[Step, ...] = (_DEPARTMENT_TABLES, _DEPARTMENT_INDEXES)

CONTROL_STEPS: tuple[Step, ...] = (
    Step(
        "001-tables",
        """
        CREATE TABLE task_routes (
            task_id    TEXT PRIMARY KEY,
            department TEXT NOT NULL,
            first_seen TEXT NOT NULL
        );
        CREATE TABLE trust_events (
            subject TEXT NOT NULL,
            item    TEXT NOT NULL,
            kind    TEXT NOT NULL,
            passed  INTEGER NOT NULL,
            day     TEXT NOT NULL,
            PRIMARY KEY (subject, item, kind)
        );
        CREATE TABLE model_events (
            subject TEXT NOT NULL,
            item    TEXT NOT NULL,
            kind    TEXT NOT NULL,
            passed  INTEGER NOT NULL,
            day     TEXT NOT NULL,
            PRIMARY KEY (subject, item, kind)
        );
        CREATE TABLE run_ledger (
            input_digest    TEXT PRIMARY KEY,
            artefact_digest TEXT NOT NULL,
            run_day         TEXT NOT NULL
        );
        """,
    ),
)


def steps_for(name: str) -> tuple[Step, ...]:
    return CONTROL_STEPS if name == CONTROL else DEPARTMENT_STEPS
