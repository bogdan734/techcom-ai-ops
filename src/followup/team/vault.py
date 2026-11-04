"""One SQLite file per department, each knowing what it is.

A file carries an application id, its own name and a ledger of the migration steps applied to
it. Opening checks all three, so a renamed copy, the old single ``state.db`` or a file written
by newer code is an error before any row is touched. ``run`` creates missing files and refuses
pending steps; applying them is an explicit ``followup team db migrate``, after a backup.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from followup.store import SCHEMA_VERSION, Store

from .access import SERVICE, authorizer, grant
from .org import SUPPORT_DEPARTMENT, UNROUTED, Org, Person
from .schema import APPLICATION_ID, BOOTSTRAP, CONTROL, Step, steps_for


class VaultError(Exception):
    """A database file is missing, foreign, drifted, newer than this code or half-migrated."""


@dataclass(frozen=True)
class DbStatus:
    name: str
    applied: tuple[str, ...]
    pending: tuple[str, ...]
    base_version: int


class Vault:
    def __init__(self, root: Path, org: Org, visibility: str) -> None:
        self.root = root
        self.org = org
        self.visibility = visibility

    def known(self) -> tuple[str, ...]:
        """Every name this vault may hold: the departments, two fixed ones and the control file."""
        fixed = {UNROUTED, SUPPORT_DEPARTMENT, CONTROL}
        return tuple(sorted(set(self.org.departments()) | fixed))

    def path(self, name: str) -> Path:
        if name not in self.known():
            raise VaultError(f"{name!r} is not a department of this org")
        return self.root / f"{name}.db"

    def names(self) -> tuple[str, ...]:
        """Names that have a file now."""
        return tuple(name for name in self.known() if self.path(name).exists())

    def create(self, name: str) -> None:
        final = self.path(name)
        if final.exists():
            raise VaultError(f"{final} already exists")
        # Built beside the final name and renamed, so a crash leaves no half-made department.
        building = final.with_name(f".{name}.creating.db")
        self._discard(building)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if name != CONTROL:
                Store(building).close()  # the base outbox tables, version and journal mode
            with closing(sqlite3.connect(building, isolation_level=None)) as db:
                db.executescript(BOOTSTRAP)
                db.execute("INSERT INTO meta (key, value) VALUES ('name', ?)", (name,))
                for seq, step in enumerate(steps_for(name), start=1):
                    self._apply(db, name, step, seq)
            os.replace(building, final)
        except BaseException:
            self._discard(building)
            raise

    def ensure(self, name: str) -> None:
        """Create the file when missing; otherwise it has to be current."""
        if not self.path(name).exists():
            self.create(name)
        else:
            self._require_current(name)

    def check(self, name: str) -> DbStatus:
        path = self.path(name)
        if not path.exists():
            raise VaultError(f"{path} does not exist")
        with closing(self._connect(path, "ro")) as db:
            app_id = db.execute("PRAGMA application_id").fetchone()[0]
            if app_id != APPLICATION_ID:
                raise VaultError(f"{path} is not a team database (application id {app_id})")
            base_version = db.execute("PRAGMA user_version").fetchone()[0]
            try:
                stored = db.execute("SELECT value FROM meta WHERE key = 'name'").fetchone()
                ledger = db.execute("SELECT seq, name, sha256 FROM team_migrations ORDER BY seq")
                applied = ledger.fetchall()
            except sqlite3.DatabaseError as exc:
                raise VaultError(f"{path} has no readable header: {exc}") from exc
        if stored is None or stored[0] != name:
            raise VaultError(f"{path} belongs to {stored[0] if stored else None!r}, not {name!r}")
        steps = steps_for(name)
        if len(applied) > len(steps):
            raise VaultError(f"{path} is newer than this code: {len(applied)} steps applied")
        for (seq, step_name, digest), step in zip(applied, steps, strict=False):
            if (seq, step_name, digest) != (steps.index(step) + 1, step.name, step.sha256):
                raise VaultError(f"{path}: step {step_name} was applied as something else")
        if name != CONTROL and base_version > SCHEMA_VERSION:
            raise VaultError(
                f"{path} has base schema {base_version}, this code knows {SCHEMA_VERSION}"
            )
        return DbStatus(
            name,
            tuple(step_name for _, step_name, _ in applied),
            tuple(step.name for step in steps[len(applied) :]),
            base_version,
        )

    def migrate(self, name: str) -> tuple[str, ...]:
        """Apply pending steps, each after a backup and in one transaction."""
        status = self.check(name)
        steps = {step.name: step for step in steps_for(name)}
        path = self.path(name)
        done = []
        for step_name in status.pending:
            step = steps[step_name]
            self._backup(path, f"{name}.before-{step.name}.db")
            with closing(self._connect(path, "rw")) as db:
                self._apply(db, name, step, len(status.applied) + len(done) + 1)
            done.append(step_name)
        return tuple(done)

    def open(
        self,
        name: str,
        viewer: Person | None = None,
        writes: frozenset[str] = frozenset(),
    ) -> sqlite3.Connection:
        """A connection under the viewer's grant; ``None`` is the service. Never creates a file."""
        self._require_current(name)
        given = (
            SERVICE if viewer is None else grant(viewer, name, self.org, self.visibility, writes)
        )
        db = self._connect(self.path(name), "ro" if given.read_only else "rw")
        db.set_authorizer(authorizer(given))
        return db

    # -- internals ---------------------------------------------------------

    def _require_current(self, name: str) -> None:
        status = self.check(name)
        if status.pending:
            raise VaultError(
                f"{self.path(name)} has pending steps {list(status.pending)}: "
                "run `followup team db migrate`"
            )

    @staticmethod
    def _connect(path: Path, mode: str) -> sqlite3.Connection:
        db = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode={mode}", uri=True, isolation_level=None
        )
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @staticmethod
    def _apply(db: sqlite3.Connection, name: str, step: Step, seq: int) -> None:
        script = (
            f"BEGIN;\n{step.sql}\n"
            f"INSERT INTO team_migrations (seq, name, sha256) "
            f"VALUES ({seq}, '{step.name}', '{step.sha256}');\nCOMMIT;"
        )
        try:
            db.executescript(script)
        except sqlite3.Error as exc:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise VaultError(f"{name}: step {step.name} failed and was rolled back: {exc}") from exc

    def _backup(self, path: Path, file_name: str) -> None:
        # A fixed name, no timestamp: a rerun overwrites the same file instead of piling up.
        target = path.with_name(file_name)
        with closing(self._connect(path, "ro")) as source, closing(sqlite3.connect(target)) as copy:
            source.backup(copy)

    @staticmethod
    def _discard(path: Path) -> None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
