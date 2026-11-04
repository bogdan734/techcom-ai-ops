import json
import sqlite3
from datetime import date

import pytest

from followup.config import ConfigError
from followup.team.calibrate import GoldItem, calibrate, cohen_kappa, load_gold, write
from followup.team.check import ModelAudit
from followup.team.schema import CONTROL_STEPS
from followup.team.trust import Ledger
from teamkit import team_settings


def item(number, label):
    return GoldItem(f"g{number}", "Q?", "must log it", ("log",), "answer", label)


def test_kappa_matches_hand_computed_0_4():
    truth = [True] * 5 + [False] * 5
    predicted = [True, True, True, True, False, True, True, False, False, False]
    # observed agreement 0.7, chance agreement 0.5
    assert cohen_kappa(truth, predicted) == pytest.approx(0.4)


def test_kappa_of_a_perfect_grader_is_one():
    labels = [True, False, True, False]
    assert cohen_kappa(labels, labels) == 1.0


def test_kappa_refuses_unequal_lengths_and_undefined_cases():
    with pytest.raises(ValueError):
        cohen_kappa([True], [True, False])
    with pytest.raises(ValueError, match="undefined"):
        cohen_kappa([True, True], [True, True])


def test_one_class_gold_is_refused(tmp_path):
    path = tmp_path / "gold.json"
    record = {"item_id": "a", "prompt": "Q", "quote": "q", "key_terms": ["q"], "answer": "a"}
    path.write_text(
        json.dumps([{**record, "label": True}, {**record, "item_id": "b", "label": True}])
    )
    with pytest.raises(ConfigError, match="both correct and incorrect"):
        load_gold(path)


def test_a_malformed_gold_file_is_a_config_error(tmp_path):
    path = tmp_path / "gold.json"
    path.write_text('[{"item_id": "a"}]')
    with pytest.raises(ConfigError, match="cannot read the gold set"):
        load_gold(path)


def test_calibration_feeds_the_model_ledger(tmp_path):
    rules = team_settings(tmp_path)
    db = sqlite3.connect(":memory:", isolation_level=None)
    for step in CONTROL_STEPS:
        db.executescript(step.sql)
    audit = ModelAudit(Ledger(db, "model_events"), "m1", rules, date(2026, 10, 21))
    gold = [item(1, True), item(2, True), item(3, False), item(4, False)]

    result = calibrate(gold, lambda g: g.label, lambda g: g.item_id != "g3" and g.label, audit)

    assert result.n == 4 and result.kappa_rules == 1.0
    assert result.agreement_model == 1.0 and result.kappa_model == 1.0
    assert [e.passed for e in audit.ledger.events("m1:grade_answer")] == [True] * 4
    # a second calibration the same day adds nothing: the events have unique keys
    calibrate(gold, lambda g: g.label, lambda g: not g.label, audit)
    assert [e.passed for e in audit.ledger.events("m1:grade_answer")] == [True] * 4


def test_a_grader_that_says_yes_to_everything_scores_zero_kappa(tmp_path):
    rules = team_settings(tmp_path)
    db = sqlite3.connect(":memory:", isolation_level=None)
    for step in CONTROL_STEPS:
        db.executescript(step.sql)
    audit = ModelAudit(Ledger(db, "model_events"), "m1", rules, date(2026, 10, 21))
    gold = [item(1, True), item(2, True), item(3, True), item(4, False)]
    result = calibrate(gold, lambda g: True, lambda g: True, audit)
    assert result.kappa_rules == 0.0 and result.kappa_model == 0.0
    assert result.agreement_model == 0.75


def test_a_model_that_was_down_leaves_the_model_figures_empty(tmp_path):
    rules = team_settings(tmp_path)
    db = sqlite3.connect(":memory:", isolation_level=None)
    for step in CONTROL_STEPS:
        db.executescript(step.sql)
    audit = ModelAudit(Ledger(db, "model_events"), "m1", rules, date(2026, 10, 21))
    gold = [item(1, True), item(2, False)]
    result = calibrate(gold, lambda g: g.label, lambda g: None, audit)
    assert (result.kappa_model, result.agreement_model) == (None, None)
    assert audit.ledger.events("m1:grade_answer") == []


def test_write_produces_stable_json(tmp_path):
    out = tmp_path / "x" / "calibration.json"
    write(
        calibrate(
            [item(1, True), item(2, False)], lambda g: g.label, lambda g: g.label, _audit(tmp_path)
        ),
        out,
    )
    assert json.loads(out.read_text())["kappa_rules"] == 1.0


def _audit(tmp_path):
    db = sqlite3.connect(":memory:", isolation_level=None)
    for step in CONTROL_STEPS:
        db.executescript(step.sql)
    return ModelAudit(Ledger(db, "model_events"), "m1", team_settings(tmp_path), date(2026, 10, 21))
