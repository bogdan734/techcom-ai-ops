"""Sampling without randomness: the same key always gives the same draw.

A run that picks a spot check, a question order or an audit sample must pick the same ones when
it is repeated, so every choice is a function of a string and never of a random generator or
the clock.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from typing import TypeVar

T = TypeVar("T")


def fraction(key: str) -> float:
    """A stable number in [0, 1) derived from ``key``."""
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) / 0x100000000


def sampled(key: str, rate: float) -> bool:
    return fraction(key) < rate


def seeded_order(items: Sequence[T], seed: str, key: Callable[[T], str]) -> list[T]:
    """``items`` in an order fixed by ``seed``; equal seeds give equal orders."""
    return sorted(
        items, key=lambda item: hashlib.sha256(f"{seed}|{key(item)}".encode()).hexdigest()
    )
