"""Settings from the environment (``.env`` is loaded by the CLI); see ``.env.example``."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from .rules import Policy

PROVIDERS = ("stub", "anthropic")


class ConfigError(ValueError):
    """Invalid settings or unreadable input: the run cannot start."""


@dataclass(frozen=True)
class Settings:
    today: date
    tasks_file: Path
    out_dir: Path
    provider: str
    model: str
    llm_timeout: float
    llm_max_retries: int
    llm_max_calls: int
    price_input_per_mtok: float
    price_output_per_mtok: float
    policy: Policy

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        def text(name: str, default: str) -> str:
            return env.get(name, "").strip() or default

        def number(name: str, default: float, kind: type = float):
            raw = text(name, str(default))
            try:
                value = kind(raw)
            except ValueError as exc:
                raise ConfigError(f"{name}={raw!r} is not a {kind.__name__}") from exc
            if value < 0:
                raise ConfigError(f"{name} must not be negative")
            return value

        provider = text("LLM_PROVIDER", "stub")
        if provider not in PROVIDERS:
            raise ConfigError(f"LLM_PROVIDER={provider!r}, expected one of {PROVIDERS}")
        if provider == "anthropic" and not env.get("ANTHROPIC_API_KEY"):
            raise ConfigError("LLM_PROVIDER=anthropic needs ANTHROPIC_API_KEY")

        return cls(
            today=parse_day(env.get("EVALUATION_DATE", "")),
            tasks_file=Path(text("TASKS_FILE", "fixtures/tasks.json")),
            out_dir=Path(text("OUT_DIR", "out")),
            provider=provider,
            model=text("LLM_MODEL", "claude-opus-5-5"),
            llm_timeout=number("LLM_TIMEOUT_SECONDS", 30.0),
            llm_max_retries=number("LLM_MAX_RETRIES", 2, int),
            llm_max_calls=number("LLM_MAX_CALLS_PER_RUN", 500, int),
            price_input_per_mtok=number("LLM_PRICE_INPUT_PER_MTOK", 4.0),
            price_output_per_mtok=number("LLM_PRICE_OUTPUT_PER_MTOK", 20.0),
            policy=Policy(
                escalate_after_days=number("ESCALATE_AFTER_DAYS", 5, int),
                stale_after_days=number("STALE_AFTER_DAYS", 5, int),
                due_soon_days=number("DUE_SOON_DAYS", 2, int),
                clarify_target=text("CLARIFY_TARGET", "project_manager"),
                escalation_target=text("ESCALATION_TARGET", "project_manager"),
                materials_target=text("MATERIALS_TARGET", "warehouse"),
            ),
        )


def parse_day(value: str) -> date:
    """``YYYY-MM-DD``, or today's date in UTC when empty."""
    if not value.strip():
        return datetime.now(UTC).date()
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ConfigError(f"{value!r} is not a YYYY-MM-DD date") from exc
