"""Scripted failures, so every reliability scenario can be reproduced on demand.

``--fault ROUTE=F1,F2xN`` queues faults for the next calls on a route, in order, and ``xN``
repeats one fault N times. The HTTP routes are served by :mod:`followup.mock_api`; the
``llm`` route wraps whichever model client is configured.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable

from .llm import Completion, LLMClient, LLMError, LLMRequest

HTTP_FAULTS = frozenset({"429", "500", "502", "503", "504", "timeout", "timeout-after-commit"})
LLM_FAULTS = frozenset({"garbage", "error"})
ROUTES = {"tasks": HTTP_FAULTS, "send": HTTP_FAULTS, "lookup": HTTP_FAULTS, "llm": LLM_FAULTS}
_REPEAT = re.compile(r"^(?P<name>.+?)x(?P<times>\d+)$")


class FaultSpecError(ValueError):
    pass


def parse_faults(specs: Iterable[str]) -> dict[str, list[str]]:
    """``["tasks=429,503", "send=503x3"]`` -> ``{"tasks": ["429", "503"], "send": [...]}``."""
    faults: dict[str, list[str]] = {}
    for spec in specs:
        route, _, items = spec.partition("=")
        allowed = ROUTES.get(route)
        if allowed is None:
            raise FaultSpecError(f"unknown fault route {route!r}, expected one of {sorted(ROUTES)}")
        for item in filter(None, (part.strip() for part in items.split(","))):
            repeat = _REPEAT.match(item)
            name, times = (repeat["name"], int(repeat["times"])) if repeat else (item, 1)
            if name not in allowed:
                raise FaultSpecError(
                    f"unknown fault {name!r} for {route}, expected one of {sorted(allowed)}"
                )
            faults.setdefault(route, []).extend([name] * times)
    return faults


class FaultyLLM:
    """Replaces a model client's next answers with scripted failures, then gets out of the way."""

    def __init__(self, client: LLMClient, faults: Iterable[str]):
        self.client = client
        self.model = client.model
        self.faults = deque(faults)

    def complete(self, request: LLMRequest) -> Completion:
        fault = self.faults.popleft() if self.faults else None
        if fault == "garbage":
            return Completion("Sure! Everything on this task looks fine to me.")
        if fault == "error":
            raise LLMError("injected provider error")
        return self.client.complete(request)
