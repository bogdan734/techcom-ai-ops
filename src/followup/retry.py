"""Bounded retries: exponential backoff with jitter, and respect for ``Retry-After``."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.5
    max_delay: float = 8.0
    sleep: Callable[[float], None] = time.sleep
    jitter: Callable[[], float] = random.random

    def delay(self, attempt: int, retry_after: float | None = None) -> float | None:
        """Seconds to wait after failed attempt ``attempt`` (1-based), or None to give up.

        A server asking for a longer pause than ``max_delay`` is obeyed by stopping: the
        next scheduled run will try again, which is cheaper than holding a worker and
        kinder than retrying early. Jitter keeps parallel workers from retrying in step.
        """
        if attempt >= self.max_attempts:
            return None
        if retry_after is not None:
            return retry_after if retry_after <= self.max_delay else None
        ceiling = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
        return ceiling / 2 + self.jitter() * ceiling / 2


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """``Retry-After`` is either delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - (now or datetime.now(UTC))).total_seconds())
