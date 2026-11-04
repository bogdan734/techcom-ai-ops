import sqlite3
from datetime import date, timedelta

import pytest

from followup.team.check import ModelAudit, consistent, keep_grounded
from followup.team.pins import keep, pinned
from followup.team.schema import CONTROL_STEPS, DEPARTMENT_STEPS
from followup.team.trust import Ledger
from teamkit import team_settings

TODAY = date(2026, 10, 21)
SPEC = "The tool must log every splice.\nLoss should stay under 0.5 dB."


def memory(steps):
    db = sqlite3.connect(":memory:", isolation_level=None)
    for step in steps:
        db.executescript(step.sql)
    return db


@pytest.fixture
def audit_for(tmp_path):
    rules = team_settings(tmp_path)
    db = memory(CONTROL_STEPS)

    def make(today=TODAY):
        return ModelAudit(Ledger(db, "model_events"), "m1", rules, today)

    return make


def fill(audit, results, feature="quiz_questions"):
    for number, passed in enumerate(results):
        later = ModelAudit(audit.ledger, audit.model, audit.rules, TODAY + timedelta(days=number))
        later.record(feature, f"i{number}", "grounding", passed)
    return ModelAudit(audit.ledger, audit.model, audit.rules, TODAY + timedelta(days=len(results)))


def test_paraphrased_quote_is_dropped():
    items = [("log every splice", 1), ("record each splice", 2), ("under 0.5 dB", 3)]
    kept, dropped = keep_grounded(items, SPEC, lambda item: item[0])
    assert [number for _, number in kept] == [1, 3] and dropped == 1


def test_a_quote_split_by_a_line_break_is_still_in_the_spec():
    kept, dropped = keep_grounded(["every splice. Loss should"], SPEC, str)
    assert kept and dropped == 0


def test_an_empty_quote_is_dropped():
    assert keep_grounded([""], SPEC, str) == ([], 1)


def test_rules_only_after_failed_model_events(audit_for):
    audit = fill(audit_for(), [False, False, False, True])
    assert audit.rules_only("quiz_questions") is True
    assert audit.rules_only("grade_answer") is False  # another feature has its own record


def test_a_good_record_stays_on_the_model(audit_for):
    assert fill(audit_for(), [True, True, True, False, True]).rules_only("quiz_questions") is False


def test_model_in_probation_is_not_rules_only(audit_for):
    assert fill(audit_for(), [False, False]).rules_only("quiz_questions") is False


def test_second_opinion_always_while_tightened(audit_for):
    audit = fill(audit_for(), [True, True, True, False, False])
    assert all(audit.second_opinion("quiz_questions", f"item-{i}") for i in range(50))


def test_second_opinion_samples_at_the_audit_rate_otherwise(audit_for):
    audit = audit_for()
    picked = sum(audit.second_opinion("quiz_questions", f"item-{i}") for i in range(400))
    assert abs(picked / 400 - 0.25) < 0.07
    assert picked == sum(audit.second_opinion("quiz_questions", f"item-{i}") for i in range(400))


def test_recording_the_same_result_twice_counts_once(audit_for):
    audit = audit_for()
    assert audit.record("grade_answer", "i1", "grounding", False) is True
    assert audit.record("grade_answer", "i1", "grounding", False) is False


def test_consistent_uses_jaccard_at_one_half():
    assert consistent(["a", "b"], ["a", "b", "c"]) is True  # 2/3
    assert consistent(["a", "b"], ["b", "c", "d"]) is False  # 1/4
    assert consistent([], []) is True


def test_pins_keep_the_first_answer():
    db = memory(DEPARTMENT_STEPS)
    first = keep(db, "quiz_questions", "TC-1|abc", {"q": 1}, "m1", TODAY)
    second = keep(db, "quiz_questions", "TC-1|abc", {"q": 2}, "m2", TODAY)
    assert first == second == {"q": 1}
    assert pinned(db, "quiz_questions", "TC-1|abc") == {"q": 1}
    assert pinned(db, "grade_answer", "TC-1|abc") is None
