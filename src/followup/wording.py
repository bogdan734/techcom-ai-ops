"""Plain-English fragments shared by decision reasons and message templates."""

from __future__ import annotations

from collections.abc import Sequence


def days(count: int) -> str:
    return f"{count} day" if count == 1 else f"{count} days"


def listing(items: Sequence[str]) -> str:
    """``["a"]`` -> ``a``; ``["a", "b", "c"]`` -> ``a, b and c``."""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
