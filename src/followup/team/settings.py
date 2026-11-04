"""Environment settings of the team layer. ``from_env`` returns ``None`` without ``ORG_FILE``."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path

from followup.config import ConfigError

from .org import Org

VISIBILITY = ("hidden", "issuers")


@dataclass(frozen=True)
class TeamSettings:
    org_file: Path
    org: Org
    trust_visibility: str
    trust_half_life_days: int
    trust_probation_tasks: int
    trust_reduced_score: float
    trust_halt_fails: int
    reduced_skip_lot: int
    quiz_max_questions: int
    quiz_pass_score: float
    audit_rate: float
    audit_model: str
    model_floor: float
    new_task_window_days: int
    chat_sla_days: int
    support_sla_days: int
    ladder_step_days: int
    calls: bool
    call_window: tuple[time, time]
    call_time: time

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> TeamSettings | None:
        org_file = env.get("ORG_FILE", "").strip()
        if not org_file:
            return None

        def text(name: str, default: str) -> str:
            return env.get(name, "").strip() or default

        def number(name: str, default: float, kind: type = float, low: float = 0, high=None):
            raw = text(name, str(default))
            try:
                value = kind(raw)
            except ValueError as exc:
                raise ConfigError(f"{name}={raw!r} is not a {kind.__name__}") from exc
            if value < low or (high is not None and value > high):
                bound = f"at least {low}" if high is None else f"between {low} and {high}"
                raise ConfigError(f"{name} must be {bound}")
            return value

        visibility = text("TRUST_VISIBILITY", "hidden")
        if visibility not in VISIBILITY:
            raise ConfigError(f"TRUST_VISIBILITY={visibility!r}, expected one of {VISIBILITY}")
        calls = text("CALLS", "false").lower()
        if calls not in ("true", "false", "1", "0"):
            raise ConfigError(f"CALLS={calls!r} is not true, false, 1 or 0")
        path = Path(org_file)
        return cls(
            org_file=path,
            org=Org.load(path),
            trust_visibility=visibility,
            trust_half_life_days=number("TRUST_HALF_LIFE_DAYS", 90, int, 1),
            trust_probation_tasks=number("TRUST_PROBATION_TASKS", 3, int),
            trust_reduced_score=number("TRUST_REDUCED_SCORE", 0.7, float, 0.01, 1),
            trust_halt_fails=number("TRUST_HALT_FAILS", 3, int, 1),
            reduced_skip_lot=number("REDUCED_SKIP_LOT", 3, int, 1),
            quiz_max_questions=number("QUIZ_MAX_QUESTIONS", 5, int, 1),
            quiz_pass_score=number("QUIZ_PASS_SCORE", 0.6, float, 0.01, 1),
            audit_rate=number("AUDIT_RATE", 0.25, float, 0, 1),
            audit_model=text("AUDIT_MODEL", ""),
            model_floor=number("MODEL_FLOOR", 0.5, float, 0, 1),
            new_task_window_days=number("NEW_TASK_WINDOW_DAYS", 2, int),
            chat_sla_days=number("CHAT_SLA_DAYS", 2, int, 1),
            support_sla_days=number("SUPPORT_SLA_DAYS", 1, int, 1),
            ladder_step_days=number("LADDER_STEP_DAYS", 1, int, 1),
            calls=calls in ("true", "1"),
            call_window=_window(text("CALL_WINDOW", "09:00-18:00")),
            call_time=_clock(env.get("CALL_TIME", "").strip()),
        )

    def call_open(self) -> bool:
        start, end = self.call_window
        return start <= self.call_time < end

    def decision_fields(self) -> dict[str, object]:
        """What can change a decision. The clock enters only as open or closed: it would
        otherwise make every run of the same input look different."""
        return {
            "org": self.org.digest,
            "trust_visibility": self.trust_visibility,
            "trust_half_life_days": self.trust_half_life_days,
            "trust_probation_tasks": self.trust_probation_tasks,
            "trust_reduced_score": self.trust_reduced_score,
            "trust_halt_fails": self.trust_halt_fails,
            "reduced_skip_lot": self.reduced_skip_lot,
            "quiz_max_questions": self.quiz_max_questions,
            "quiz_pass_score": self.quiz_pass_score,
            "audit_rate": self.audit_rate,
            "audit_model": self.audit_model,
            "model_floor": self.model_floor,
            "new_task_window_days": self.new_task_window_days,
            "chat_sla_days": self.chat_sla_days,
            "support_sla_days": self.support_sla_days,
            "ladder_step_days": self.ladder_step_days,
            "calls": self.calls,
            "call_open": self.calls and self.call_open(),
        }


def _window(raw: str) -> tuple[time, time]:
    start, _, end = raw.partition("-")
    window = (_hhmm("CALL_WINDOW", start), _hhmm("CALL_WINDOW", end))
    if window[0] >= window[1]:
        raise ConfigError(f"CALL_WINDOW={raw!r}: the start must be before the end, same day")
    return window


def _clock(raw: str) -> time:
    if raw:
        return _hhmm("CALL_TIME", raw)
    # Read once, here, so no decision code ever asks the clock.
    return datetime.now(UTC).time().replace(second=0, microsecond=0)


def _hhmm(name: str, raw: str) -> time:
    try:
        return time.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name}: {raw!r} is not HH:MM") from exc
