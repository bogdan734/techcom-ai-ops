"""``followup team calibrate``: how often does the grader agree with a person?

It scores the rule grader and the model grader on answers a person has already labelled, prints
Cohen's kappa for each and writes ``calibration.json`` next to the state. The model's agreement
with the labels also goes on its ledger, so a grader that keeps getting it wrong drops to rules.
"""

from __future__ import annotations

from pathlib import Path

from followup.config import ConfigError
from followup.llm import LLMGateway
from followup.pipeline import make_llm

from .calibrate import GoldItem, calibrate, load_gold, write
from .check import ModelAudit
from .context import Context
from .grading import SYSTEM, ask_grade, evidence_grounded, rule_grade
from .quiz import Question
from .schema import CONTROL
from .trust import Ledger


def run_calibration(ctx: Context, gold_file: str) -> int:
    gold = load_gold(Path(gold_file))
    control = ctx.vault.open(CONTROL) if ctx.vault.path(CONTROL).exists() else None
    if control is None:
        raise ConfigError("no state yet: start a run first, so the model ledger exists")
    gateway = LLMGateway(make_llm(ctx.settings), ctx.settings.llm_max_calls)
    audit = ModelAudit(Ledger(control, "model_events"), gateway.model, ctx.team, ctx.today)

    def question(item: GoldItem) -> Question:
        return Question(item.item_id, item.prompt, item.quote, item.key_terms, "rules")

    def by_rules(item: GoldItem) -> bool:
        return rule_grade(question(item), item.answer, ctx.team.quiz_pass_score)

    def by_model(item: GoldItem) -> bool | None:
        result = ask_grade(gateway, "grade_answer", SYSTEM, question(item), item.answer)
        if result is None or not evidence_grounded(result, item.answer):
            return None
        return result.correct

    result = calibrate(gold, by_rules, by_model, audit)
    path = ctx.settings.out_dir / "calibration.json"
    write(result, path)
    control.close()
    print(f"{result.n} labelled answers, model {gateway.model}")
    print(f"  rules   kappa {_fmt(result.kappa_rules)}")
    print(f"  model   kappa {_fmt(result.kappa_model)}, agreement {_fmt(result.agreement_model)}")
    print(f"written to {path}")
    return 0


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"
