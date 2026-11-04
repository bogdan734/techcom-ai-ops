import sqlite3
from datetime import date

import pytest

from followup.llm import LLMGateway
from followup.llm.base import LLMError
from followup.team.check import ModelAudit
from followup.team.quiz import (
    TEACH_BACK,
    QuizMaker,
    issue,
    open_quiz,
    rule_questions,
    select,
)
from followup.team.schema import CONTROL_STEPS, DEPARTMENT_STEPS
from followup.team.spec_terms import contains_verbatim
from followup.team.trust import Ledger, Plan
from teamkit import KindLLM, team_settings

TODAY = date(2026, 10, 21)
SPEC = (
    "Install four handholes along Elm Street. "
    "The crew must record GPS coordinates for each handhole. "
    "Splice loss should stay under 0.5 dB. "
    "Photos need to be uploaded within 48 hours."
)
GROUNDED = {
    "prompt": "What splice loss limit applies?",
    "quote": "Splice loss should stay under 0.5 dB.",
    "key_terms": ["0.5", "fiber"],
}
INVENTED = {
    "prompt": "Who signs off the work?",
    "quote": "The foreman signs off every handhole.",
    "key_terms": ["foreman"],
}


def memory(steps):
    db = sqlite3.connect(":memory:", isolation_level=None)
    for step in steps:
        db.executescript(step.sql)
    return db


def reply(*items):
    import json

    return json.dumps({"questions": list(items)})


@pytest.fixture
def env(tmp_path):
    def build(llm, **settings):
        rules = team_settings(tmp_path, **{"AUDIT_RATE": "0", **settings})
        audit = ModelAudit(Ledger(memory(CONTROL_STEPS), "model_events"), "fake", rules, TODAY)
        db = memory(DEPARTMENT_STEPS)
        return QuizMaker(LLMGateway(llm, max_calls=50), db, audit), db, audit

    return build


def test_rule_questions_start_with_teach_back_and_cloze_the_requirements():
    questions = rule_questions(SPEC)
    assert questions[0].question_id == TEACH_BACK
    assert questions[0].quote == "Install four handholes along Elm Street."
    prompts = [q.prompt for q in questions[1:]]
    assert any("record GPS ____ for each ____" in p for p in prompts)
    assert any("under ____" in p for p in prompts)
    assert any("within ____" in p for p in prompts)
    assert all(contains_verbatim(SPEC, q.quote) for q in questions)
    assert all(term in q.quote.lower() for q in questions for term in q.key_terms)


def test_a_spec_without_sentences_has_no_questions():
    assert rule_questions("  \n ") == []


def test_key_terms_outside_quote_dropped(env):
    maker, _, _ = env(KindLLM({"quiz_questions": [reply(GROUNDED, INVENTED)]}))
    model = [q for q in maker.candidates("TC-1", SPEC, False) if q.source == "model"]
    assert len(model) == 1 and model[0].key_terms == ("0.5",)  # "fiber" is not in the quote
    assert model[0].quote in SPEC


def test_a_question_with_no_term_in_its_quote_is_dropped(env):
    odd = {**GROUNDED, "key_terms": ["fiber"]}
    maker, _, _ = env(KindLLM({"quiz_questions": [reply(odd)]}))
    assert not [q for q in maker.candidates("TC-1", SPEC, False) if q.source == "model"]


def test_paraphrase_costs_the_model_a_grounding_record(env):
    maker, _, audit = env(KindLLM({"quiz_questions": [reply(GROUNDED, INVENTED)]}))
    maker.candidates("TC-1", SPEC, False)
    kinds = {e.kind: e.passed for e in audit.ledger.events("fake:quiz_questions")}
    assert kinds["grounding"] is False


def test_rerun_asks_the_model_nothing(env):
    llm = KindLLM({"quiz_questions": [reply(GROUNDED)]})
    maker, _, _ = env(llm)
    first = maker.candidates("TC-1", SPEC, False)
    assert maker.candidates("TC-1", SPEC, False) == first
    assert len(llm.requests["quiz_questions"]) == 1


def test_a_changed_spec_is_a_new_question_set(env):
    llm = KindLLM({"quiz_questions": [reply(GROUNDED)]})
    maker, _, _ = env(llm)
    maker.candidates("TC-1", SPEC, False)
    maker.candidates("TC-1", SPEC + " Label every duct.", False)
    assert len(llm.requests["quiz_questions"]) == 2


def test_flagged_spec_never_reaches_the_model(env):
    llm = KindLLM()
    maker, _, _ = env(llm)
    assert all(q.source == "rules" for q in maker.candidates("TC-1", SPEC, True))
    assert not llm.requests


def test_model_down_means_rules_and_nothing_pinned(env):
    llm = KindLLM({"quiz_questions": [LLMError("down", fatal=True)]})
    maker, db, _ = env(llm)
    assert all(q.source == "rules" for q in maker.candidates("TC-1", SPEC, False))
    assert db.execute("SELECT count(*) FROM pins").fetchone() == (0,)


def test_a_model_on_a_bad_record_is_not_asked(env):
    llm = KindLLM()
    maker, _, audit = env(llm)
    for n in range(4):
        audit.record("quiz_questions", f"i{n}", "grounding", False)
    assert all(q.source == "rules" for q in maker.candidates("TC-1", SPEC, False))
    assert not llm.requests


def test_inconsistent_second_answer_keeps_only_what_both_gave(tmp_path):
    other = {
        **GROUNDED,
        "quote": "Photos need to be uploaded within 48 hours.",
        "key_terms": ["48"],
    }
    llm = KindLLM({"quiz_questions": [reply(GROUNDED), reply(other)]})
    rules = team_settings(tmp_path, AUDIT_RATE="1")
    audit = ModelAudit(Ledger(memory(CONTROL_STEPS), "model_events"), "fake", rules, TODAY)
    maker = QuizMaker(LLMGateway(llm, max_calls=50), memory(DEPARTMENT_STEPS), audit)
    assert not [q for q in maker.candidates("TC-1", SPEC, False) if q.source == "model"]
    assert audit.ledger.events("fake:quiz_questions")[0].kind == "consistency"


def test_selection_is_seeded_and_teach_back_first():
    candidates = rule_questions(SPEC)
    picked = select(candidates, Plan(3, 5), "eve|TC-1")
    assert picked[0].question_id == TEACH_BACK and len(picked) == 3
    assert picked == select(candidates, Plan(3, 5), "eve|TC-1")
    others = {
        tuple(q.question_id for q in select(candidates, Plan(2, 5), f"s{n}")) for n in range(12)
    }
    assert len(others) > 1  # a different seed picks a different follow-up question
    assert [q.question_id for q in select(candidates, Plan(1, 2), "x")] == [TEACH_BACK]


def test_issue_is_first_writer_wins():
    db = memory(DEPARTMENT_STEPS)
    questions = rule_questions(SPEC)
    args = {
        "task_id": "TC-1",
        "person": "eve",
        "issuer": "max",
        "spec": SPEC,
        "level": "normal",
        "day": TODAY,
    }
    first = issue(db, questions=questions[:2], **args)
    second = issue(db, questions=questions, **{**args, "level": "tightened"})
    assert second == first and len(first.questions) == 2 and first.level == "normal"
    assert open_quiz(db, "TC-1", "eve") == first
    assert open_quiz(db, "TC-1", "finn") is None
