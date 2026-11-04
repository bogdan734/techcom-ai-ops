"""Fields the base task model does not carry, read from the raw records by task id.

``Task`` stays as it is: its fingerprint feeds every stored action id, so a new field there
would re-identify everything already sent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
from typing import Any

from followup.config import ConfigError

from .org import Capability, Org


@dataclass(frozen=True)
class TaskExtras:
    task_id: str
    spec: str
    assigned_by: str | None  # person id of the issuer
    code_path: str | None  # relative to the code root
    created: date | None
    group: str | None = None  # the id the parts of a task given to several people share


def load_extras(records: Sequence[Any], org: Org) -> dict[str, TaskExtras]:
    """Extras for every record that carries any; malformed extras stop the run."""
    found: dict[str, TaskExtras] = {}
    for position, record in enumerate(records):
        task_id = _task_id(record)
        if task_id is None:
            continue  # the normaliser already reports records without an identity
        extras = _extras(position, task_id, record, org)
        if extras is None:
            continue
        previous = found.get(task_id)
        if previous is not None and previous != extras:
            raise ConfigError(f"{task_id}: two records carry different spec fields")
        found[task_id] = extras
    return found


def _task_id(record: Any) -> str | None:
    if not isinstance(record, Mapping):
        return None
    value = record.get("task_id")
    if not isinstance(value, str | int) or isinstance(value, bool):
        return None
    return " ".join(str(value).split()).upper() or None


def _extras(position: int, task_id: str, record: Mapping[str, Any], org: Org) -> TaskExtras | None:
    if not any(record.get(name) is not None for name in _FIELDS):
        return None
    where = f"record #{position} ({task_id})"
    return TaskExtras(
        task_id=task_id,
        spec=_spec(where, record.get("spec")),
        assigned_by=_issuer(where, record.get("assigned_by"), org),
        code_path=_code_path(where, record.get("code_path")),
        created=_created(where, record.get("created")),
        group=_group(where, record.get("group")),
    )


_FIELDS = ("spec", "assigned_by", "code_path", "created", "group")


def _spec(where: str, value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{where}: spec must be text")
    return value.strip()


def _issuer(where: str, value: Any, org: Org) -> str | None:
    if value is None:
        return None
    person = org.find(value) if isinstance(value, str) else None
    if person is None:
        raise ConfigError(f"{where}: assigned_by {value!r} is not in the org")
    if not org.can(person.id, Capability.ISSUE_TASKS):
        raise ConfigError(f"{where}: {person.id} ({person.role}) cannot issue tasks")
    return person.id


def _code_path(where: str, value: Any) -> str | None:
    if value is None:
        return None
    path = PurePosixPath(value) if isinstance(value, str) else None
    if path is None or path.is_absolute() or ".." in path.parts or not value.strip():
        raise ConfigError(f"{where}: code_path must be a relative path without '..'")
    return path.as_posix()


def _group(where: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: group must be the id the parts of the task share")
    return " ".join(value.split()).upper()


def _created(where: str, value: Any) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value) if isinstance(value, str) else _bad(where)
    except ValueError:
        return _bad(where)


def _bad(where: str) -> date:
    raise ConfigError(f"{where}: created must be a YYYY-MM-DD date")
