"""Anthropic Messages API adapter.

Structured outputs constrain the answer to the request's JSON schema at the API, and the
gateway validates it again locally: the schema is a contract, not a guarantee we rely on.
"""

from __future__ import annotations

import anthropic

from .base import Completion, LLMError, LLMRequest

# Retrying cannot fix a wrong key, missing access, an unknown model or a malformed request.
_FATAL = (
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.NotFoundError,
    anthropic.BadRequestError,
)


class AnthropicClient:
    def __init__(
        self,
        model: str,
        timeout: float,
        max_retries: int,
        client: anthropic.Anthropic | None = None,
    ):
        self.model = model
        # The SDK retries 408/409/429/5xx and connection errors with backoff itself.
        self._client = client or anthropic.Anthropic(timeout=timeout, max_retries=max_retries)

    def complete(self, request: LLMRequest) -> Completion:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=request.system,
                messages=[{"role": "user", "content": request.prompt}],
                output_config={
                    # Classification and two-sentence drafts gain nothing from long thinking.
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": request.schema},
                },
            )
        except _FATAL as exc:
            raise LLMError(f"{type(exc).__name__}: {exc.message}", fatal=True) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"HTTP {exc.status_code} after SDK retries") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"{type(exc).__name__} after SDK retries") from exc

        if response.stop_reason == "refusal":
            raise LLMError("the model declined to answer")
        if response.stop_reason == "max_tokens":
            raise LLMError("the answer was cut off at max_tokens")
        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            raise LLMError("the answer has no text block")
        return Completion(text, response.usage.input_tokens, response.usage.output_tokens)
