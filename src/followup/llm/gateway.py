"""Validated answers from a model, or a clear "no answer".

Every model output is parsed as JSON and validated against a strict pydantic model plus
an optional semantic check. A rejected answer gets one repair attempt with the reasons;
after that the caller falls back to its deterministic path. The gateway also enforces a
per-run call budget and stops calling a failing provider.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ..audit import emit
from ..retry import CircuitBreaker
from .base import LLMClient, LLMError, LLMRequest

Answer = TypeVar("Answer", bound=BaseModel)
Check = Callable[[Answer], list[str]]

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


class LLMUnavailable(Exception):
    """No validated answer; the caller must use its deterministic fallback."""


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    rejected_answers: int = 0
    provider_errors: int = 0


class LLMGateway:
    def __init__(
        self,
        client: LLMClient,
        max_calls: int,
        breaker: CircuitBreaker | None = None,
        attempts: int = 2,
    ):
        self.client = client
        self.max_calls = max_calls
        self.breaker = breaker or CircuitBreaker()
        self.attempts = attempts
        self.usage = Usage()

    @property
    def model(self) -> str:
        return self.client.model

    def ask(
        self, request: LLMRequest, answer_type: type[Answer], check: Check | None = None
    ) -> Answer:
        problems: list[str] = []
        for attempt in range(1, self.attempts + 1):
            self._ensure_available(request)
            prompt = request.prompt if not problems else _repair_prompt(request.prompt, problems)
            self.usage.calls += 1
            try:
                completion = self.client.complete(replace(request, prompt=prompt))
            except LLMError as exc:
                self.usage.provider_errors += 1
                self.breaker.record_failure(fatal=exc.fatal)
                emit(
                    "llm_error",
                    f"{request.kind}: provider error: {exc}",
                    level=logging.WARNING,
                    kind=request.kind,
                    model=self.model,
                    error=str(exc),
                    fatal=exc.fatal,
                )
                raise LLMUnavailable(str(exc)) from exc
            self.breaker.record_success()
            self.usage.input_tokens += completion.input_tokens
            self.usage.output_tokens += completion.output_tokens
            emit(
                "llm_call",
                f"{request.kind}: answered ({completion.input_tokens}+"
                f"{completion.output_tokens} tokens)",
                level=logging.DEBUG,
                kind=request.kind,
                model=self.model,
                attempt=attempt,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
            )
            answer, problems = _validate(completion.text, answer_type, check)
            if answer is not None:
                return answer
            self.usage.rejected_answers += 1
            emit(
                "llm_rejected",
                f"{request.kind}: answer rejected: {'; '.join(problems)}",
                level=logging.WARNING,
                kind=request.kind,
                model=self.model,
                attempt=attempt,
                problems=problems,
            )
        raise LLMUnavailable("answer failed validation: " + "; ".join(problems))

    def _ensure_available(self, request: LLMRequest) -> None:
        if self.breaker.open:
            raise LLMUnavailable("provider disabled for this run after repeated errors")
        if self.usage.calls >= self.max_calls:
            emit(
                "llm_budget",
                f"{request.kind}: call budget of {self.max_calls} exhausted",
                level=logging.WARNING,
                kind=request.kind,
            )
            raise LLMUnavailable(f"call budget of {self.max_calls} exhausted")


def _validate(
    text: str, answer_type: type[Answer], check: Check | None
) -> tuple[Answer | None, list[str]]:
    raw = text.strip()
    fenced = _FENCE.match(raw)
    try:
        data = json.loads(fenced.group(1) if fenced else raw)
    except ValueError as exc:
        return None, [f"not valid JSON ({exc})"]
    try:
        answer = answer_type.model_validate(data)
    except ValidationError as exc:
        return None, [
            f"{'.'.join(str(part) for part in error['loc']) or 'answer'}: {error['msg']}"
            for error in exc.errors()
        ]
    problems = check(answer) if check else []
    return (None, problems) if problems else (answer, [])


def _repair_prompt(prompt: str, problems: list[str]) -> str:
    reasons = "\n".join(f"- {problem}" for problem in problems)
    return (
        f"{prompt}\n\nYour previous answer was rejected for these reasons:\n{reasons}\n"
        "Answer again with JSON that fixes every one of them."
    )
