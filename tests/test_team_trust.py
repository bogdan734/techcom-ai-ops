import sqlite3
from datetime import date, timedelta

import pytest

from followup.team.sampling import fraction, sampled, seeded_order
from followup.team.schema import CONTROL_STEPS
from followup.team.trust import (
    REVIEW_CLEARED,
    Event,
    Ledger,
    Level,
    Plan,
    Standing,
    decayed_score,
    plan_for,
    standing,
    wilson_lower,
)
from teamkit import team_settings

DAY0 = date(2026, 10, 1)


@pytest.fixture
def rules(tmp_path):
    return team_settings(tmp_path)


def results(*passed, start=DAY0, kind="quiz"):
    """One result per day, in order."""
    return [
        Event("eve", f"TC-{i:03d}", kind, ok, start + timedelta(days=i))
        for i, ok in enumerate(passed)
    ]


def level_after(rules, *passed):
    events = results(*passed)
    return standing(events, events[-1].day, rules).level


def test_wilson_matches_reference_values():
    assert wilson_lower(10, 10) == pytest.approx(0.7225, abs=1e-4)
    assert wilson_lower(5, 5) == pytest.approx(0.5655, abs=1e-4)


def test_wilson_is_zero_without_observations():
    assert wilson_lower(0, 0) == 0.0


def test_a_short_clean_record_scores_below_a_long_one():
    assert wilson_lower(3, 3) < wilson_lower(30, 30) < 1.0


def test_old_failure_weighs_less_than_a_new_one():
    old_fail = [
        Event("a", "1", "quiz", False, DAY0),
        Event("a", "2", "quiz", True, DAY0 + timedelta(days=180)),
    ]
    new_fail = [
        Event("a", "1", "quiz", True, DAY0),
        Event("a", "2", "quiz", False, DAY0 + timedelta(days=180)),
    ]
    today = DAY0 + timedelta(days=181)
    assert decayed_score(old_fail, today, 90) > decayed_score(new_fail, today, 90)


def test_score_depends_on_today_argument_only():
    events = results(True, True, True, True)
    now = events[-1].day
    assert decayed_score(events, now, 90) == decayed_score(events, now, 90)
    assert decayed_score(events, now + timedelta(days=365), 90) < decayed_score(events, now, 90)


def test_recording_an_event_twice_keeps_one_row():
    db = sqlite3.connect(":memory:")
    for step in CONTROL_STEPS:
        db.executescript(step.sql)
    ledger = Ledger(db, "trust_events")
    event = Event("eve", "TC-1", "quiz", True, DAY0)
    assert ledger.record(event) is True
    assert ledger.record(event) is False
    assert ledger.events("eve") == [event]


def test_the_ledger_refuses_an_unknown_table():
    with pytest.raises(ValueError):
        Ledger(sqlite3.connect(":memory:"), "users")  # type: ignore[arg-type]


def test_probation_failure_keeps_the_standard_plan(rules):
    events = results(False, False)  # two fails, but only two results: still on probation
    current = standing(events, events[-1].day, rules)
    assert current.level is Level.NORMAL and current.probation
    assert plan_for(current, "eve|TC-9", rules) == Plan(3, 5)


def test_two_fails_in_five_tighten(rules):
    assert level_after(rules, True, True, False, True, False) is Level.TIGHTENED
    assert level_after(rules, True, True, False, True, True) is Level.NORMAL


def test_ten_clean_results_reach_reduced(rules):
    # nine fresh passes only just miss 0.7 once a few days of decay are counted
    assert level_after(rules, *[True] * 8) is Level.NORMAL
    assert level_after(rules, *[True] * 10) is Level.REDUCED


def test_one_fail_on_reduced_returns_to_normal(rules):
    assert level_after(rules, *[True] * 10, False) is Level.NORMAL


def test_a_quiet_good_record_decays_out_of_reduced(rules):
    events = results(*[True] * 12)
    assert standing(events, events[-1].day, rules).level is Level.REDUCED
    assert standing(events, events[-1].day + timedelta(days=400), rules).level is Level.NORMAL


def test_five_passes_in_a_row_leave_tightened(rules):
    assert level_after(rules, True, True, False, False, True, True, True, True) is Level.TIGHTENED
    assert level_after(rules, True, True, False, False, *[True] * 5) is Level.NORMAL


def test_halt_after_n_fails_on_tightened(rules):
    # two fails tighten; three more (the default) halt
    assert level_after(rules, True, False, False, False, False) is Level.TIGHTENED
    assert level_after(rules, True, False, False, False, False, False) is Level.HALTED


def test_review_cleared_lifts_a_halt(rules):
    halted = results(True, False, False, False, False, False)
    cleared = Event("eve", "review", REVIEW_CLEARED, True, halted[-1].day + timedelta(days=1))
    now = cleared.day
    assert standing(halted, now, rules).level is Level.HALTED
    after = standing([*halted, cleared], now, rules)
    assert after.level is Level.NORMAL and after.events == 6


def test_a_clearance_without_a_halt_changes_nothing(rules):
    events = results(True, True, True)
    cleared = Event("eve", "review", REVIEW_CLEARED, True, events[-1].day)
    assert standing([*events, cleared], events[-1].day, rules) == standing(
        events, events[-1].day, rules
    )


def test_plans_by_level(rules):
    base = Standing(Level.NORMAL, 0.5, 4, False)
    assert plan_for(Standing(Level.TIGHTENED, 0.2, 9, False), "s", rules) == Plan(5, None)
    assert plan_for(base, "s", rules) == Plan(3, 5)
    assert plan_for(Standing(Level.HALTED, 0.2, 9, False), "s", rules) is None


def test_reduced_plan_follows_the_skip_lot(rules):
    reduced = Standing(Level.REDUCED, 0.9, 20, False)
    plans = [plan_for(reduced, f"eve|TC-{i}", rules) for i in range(300)]
    assert set(plans) == {None, Plan(1, 2)}
    assert plans == [plan_for(reduced, f"eve|TC-{i}", rules) for i in range(300)]


def test_skip_lot_is_stable_and_near_one_in_n():
    hits = sum(sampled(f"id-{i}", 1 / 3) for i in range(300))
    assert abs(hits / 300 - 1 / 3) < 0.08
    assert fraction("x") == fraction("x") and 0 <= fraction("x") < 1


def test_seeded_order_is_a_stable_permutation():
    items = ["a", "b", "c", "d", "e"]
    first = seeded_order(items, "seed-1", str)
    assert first == seeded_order(items, "seed-1", str)
    assert sorted(first) == items
    assert first != seeded_order(items, "seed-2", str) or first != seeded_order(
        items, "seed-3", str
    )
