"""Is the grader any good? Score it against answers a person has already labelled.

Cohen's kappa corrects raw agreement for chance, so a grader that says "pass" to everything
does not look good on a set that is mostly passes. The gold set has to hold both classes. The
result feeds the model ledger, and a grader below the floor drops to rules.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from followup.config import ConfigError

from .check import ModelAudit

FEATURE = "grade_answer"


@dataclass(frozen=True)
class GoldItem:
    item_id: str
    prompt: str
    quote: str
    key_terms: tuple[str, ...]
    answer: str
    label: bool  # did a person judge the answer correct


@dataclass(frozen=True)
class Calibration:
    n: int
    kappa_rules: float | None
    kappa_model: float | None
    agreement_model: float | None


def load_gold(path: Path) -> list[GoldItem]:
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
        items = [
            GoldItem(
                str(r["item_id"]),
                str(r["prompt"]),
                str(r["quote"]),
                tuple(str(t) for t in r["key_terms"]),
                str(r["answer"]),
                bool(r["label"]),
            )
            for r in records
        ]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigError(f"cannot read the gold set {path}: {exc!r}") from exc
    if len({item.label for item in items}) < 2:
        raise ConfigError(f"{path}: the gold set needs both correct and incorrect answers")
    if len({item.item_id for item in items}) != len(items):
        raise ConfigError(f"{path}: item ids must be unique")
    return items


def cohen_kappa(truth: Sequence[bool], predicted: Sequence[bool]) -> float:
    if len(truth) != len(predicted) or not truth:
        raise ValueError("kappa needs two equally long, non-empty label lists")
    n = len(truth)
    observed = sum(t == p for t, p in zip(truth, predicted, strict=True)) / n
    expected = sum(
        (truth.count(value) / n) * (predicted.count(value) / n) for value in (True, False)
    )
    if expected == 1:
        raise ValueError("kappa is undefined when both lists hold one class")
    return (observed - expected) / (1 - expected)


def calibrate(
    gold: Sequence[GoldItem],
    rule_grade: Callable[[GoldItem], bool],
    model_grade: Callable[[GoldItem], bool | None],
    audit: ModelAudit,
) -> Calibration:
    truth = [item.label for item in gold]
    by_rules = [rule_grade(item) for item in gold]
    answered: list[tuple[GoldItem, bool]] = []
    for item in gold:
        verdict = model_grade(item)
        if verdict is None:
            continue  # the model was unavailable for this item; it proves nothing either way
        answered.append((item, verdict))
        audit.record(FEATURE, item.item_id, "calibration", verdict == item.label)
    model_truth = [item.label for item, _ in answered]
    model_said = [verdict for _, verdict in answered]
    return Calibration(
        n=len(gold),
        kappa_rules=_kappa(truth, by_rules),
        kappa_model=_kappa(model_truth, model_said) if answered else None,
        agreement_model=(
            sum(t == p for t, p in zip(model_truth, model_said, strict=True)) / len(answered)
            if answered
            else None
        ),
    )


def write(calibration: Calibration, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(calibration), indent=2, sort_keys=True) + "\n", "utf-8")


def _kappa(truth: Sequence[bool], predicted: Sequence[bool]) -> float | None:
    try:
        return cohen_kappa(truth, predicted)
    except ValueError:
        return None
