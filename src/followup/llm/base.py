"""Provider-neutral contract between the service and a language model."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class LLMRequest:
    kind: str  # "read_note" or "draft_message"
    system: str
    prompt: str
    schema: dict[str, Any]  # JSON schema the answer must follow
    facts: Mapping[str, Any]  # structured inputs the prompt was rendered from


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


class LLMError(Exception):
    """The provider produced no answer. ``fatal`` means retrying cannot help."""

    def __init__(self, message: str, *, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


class LLMClient(Protocol):
    model: str

    def complete(self, request: LLMRequest) -> Completion: ...
