"""Who works here and who answers to whom: the one source of roles, departments and routing."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from followup.config import ConfigError

# Work with no owner, or an owner the org does not list, lands here instead of in a guess.
UNROUTED = "_unrouted"
SUPPORT_DEPARTMENT = "support"

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
# A leading underscore is reserved for internal files (_unrouted, _control).
_DEPARTMENT = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_PHONE = re.compile(r"^\+[0-9]{7,15}$")


class Role(StrEnum):
    BOSS = "boss"
    SENIOR_MANAGER = "senior_manager"
    MANAGER = "manager"
    ENGINEER = "engineer"
    FIELD = "field"
    SUPPORT = "support"
    # Mailboxes the base rules already address; they receive messages and hold no powers.
    PROJECT_MANAGER = "project_manager"
    WAREHOUSE = "warehouse"


class Capability(StrEnum):
    ISSUE_TASKS = "issue_tasks"
    SEE_TRUST = "see_trust"
    READ_OTHER_DEPARTMENTS = "read_other_departments"
    ANSWER_SUPPORT = "answer_support"


_ISSUER = frozenset({Capability.ISSUE_TASKS, Capability.SEE_TRUST})
ROLE_CAPABILITIES: Mapping[Role, frozenset[Capability]] = {
    Role.BOSS: _ISSUER | {Capability.READ_OTHER_DEPARTMENTS},
    Role.SENIOR_MANAGER: _ISSUER | {Capability.READ_OTHER_DEPARTMENTS},
    Role.MANAGER: _ISSUER,
    Role.ENGINEER: frozenset(),
    Role.FIELD: frozenset(),
    Role.SUPPORT: frozenset({Capability.ANSWER_SUPPORT}),
    Role.PROJECT_MANAGER: frozenset(),
    Role.WAREHOUSE: frozenset(),
}


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    role: Role
    department: str
    reports_to: str | None = None
    phone: str | None = None
    call_consent: bool = False


class Org:
    """Validated people and the reporting tree. Built once, never changed."""

    def __init__(self, people: Sequence[Person]) -> None:
        self.people = tuple(sorted(people, key=lambda person: person.id))
        self._by_id = {person.id: person for person in self.people}
        self._by_name = {person.name: person for person in self.people}
        self._check()
        self.digest = _digest(
            [
                [p.id, p.name, p.role.value, p.department, p.reports_to, p.phone, p.call_consent]
                for p in self.people
            ]
        )

    @classmethod
    def load(cls, path: Path) -> Org:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot read the org file {path}: {exc}") from exc
        records = document.get("people") if isinstance(document, dict) else None
        if not isinstance(records, list):
            raise ConfigError(f'{path}: expected an object with a "people" list')
        return cls.from_records(records)

    @classmethod
    def from_records(cls, records: Sequence[Any]) -> Org:
        return cls([_person(position, record) for position, record in enumerate(records)])

    def person(self, pid: str) -> Person:
        found = self._by_id.get(pid)
        if found is None:
            raise ConfigError(f"unknown person {pid!r}")
        return found

    def find(self, reference: str | None) -> Person | None:
        """A person by id, or else by name; ``None`` when nobody matches."""
        if reference is None:
            return None
        return self._by_id.get(reference) or self._by_name.get(reference)

    def by_name(self, name: str | None) -> Person | None:
        return self._by_name.get(name) if name else None

    def department_of(self, owner: str | None) -> str:
        person = self.by_name(owner)
        return person.department if person else UNROUTED

    def chain_above(self, pid: str) -> tuple[Person, ...]:
        """Direct supervisor first, the boss last."""
        chain: list[Person] = []
        current = self.person(pid).reports_to
        while current is not None:
            supervisor = self.person(current)
            chain.append(supervisor)
            current = supervisor.reports_to
        return tuple(chain)

    def below(self, pid: str) -> frozenset[str]:
        """Everyone who answers to ``pid`` directly or through others, never ``pid`` itself."""
        found: set[str] = set()
        frontier = [pid]
        while frontier:
            head = frontier.pop()
            for person in self.people:
                if person.reports_to == head and person.id not in found:
                    found.add(person.id)
                    frontier.append(person.id)
        return frozenset(found)

    def with_role(self, role: Role) -> tuple[Person, ...]:
        return tuple(person for person in self.people if person.role is role)

    def departments(self) -> tuple[str, ...]:
        return tuple(sorted({person.department for person in self.people}))

    def can(self, pid: str, capability: Capability) -> bool:
        return capability in ROLE_CAPABILITIES[self.person(pid).role]

    def _check(self) -> None:
        if len(self._by_id) != len(self.people):
            raise ConfigError("org: person ids must be unique")
        if len(self._by_name) != len(self.people):
            raise ConfigError("org: person names must be unique")
        bosses = [person for person in self.people if person.role is Role.BOSS]
        if len(bosses) != 1:
            raise ConfigError(f"org: expected exactly one boss, found {len(bosses)}")
        for person in self.people:
            if person.reports_to is None and person.role is not Role.BOSS:
                raise ConfigError(f"org: {person.id} has no reports_to")
            if person.reports_to is not None and person.reports_to not in self._by_id:
                raise ConfigError(f"org: {person.id} reports to unknown {person.reports_to!r}")
        for person in self.people:
            seen = {person.id}
            current = person.reports_to
            while current is not None:
                if current in seen:
                    raise ConfigError(f"org: reports_to cycle through {person.id}")
                seen.add(current)
                current = self._by_id[current].reports_to


def _person(position: int, record: Any) -> Person:
    if not isinstance(record, Mapping):
        raise ConfigError(f"org: person #{position} is not an object")

    def field(name: str) -> str:
        value = record.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"org: person #{position} needs a text {name!r}")
        return value.strip()

    pid, department = field("id"), field("department")
    if not _ID.match(pid):
        raise ConfigError(f"org: id {pid!r} has characters outside A-Z a-z 0-9 _ . -")
    if not _DEPARTMENT.match(department):
        raise ConfigError(
            f"org: department {department!r} must be lower case letters, digits, - and _, "
            "and cannot start with an underscore"
        )
    try:
        role = Role(field("role"))
    except ValueError as exc:
        raise ConfigError(f"org: {pid} has unknown role {record.get('role')!r}") from exc
    reports_to = record.get("reports_to")
    if reports_to is not None and not isinstance(reports_to, str):
        raise ConfigError(f"org: {pid} reports_to must be a person id or null")
    phone = record.get("phone")
    if phone is not None and (not isinstance(phone, str) or not _PHONE.match(phone)):
        raise ConfigError(f"org: {pid} phone must look like +15551234567")
    consent = record.get("call_consent", False)
    if not isinstance(consent, bool):
        raise ConfigError(f"org: {pid} call_consent must be true or false")
    if consent and phone is None:
        raise ConfigError(f"org: {pid} agrees to calls but has no phone")
    return Person(pid, field("name"), role, department, reports_to, phone, consent)


def _digest(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
