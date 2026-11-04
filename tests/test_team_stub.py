import json
import sqlite3
from datetime import date

import pytest

from followup.llm import LLMGateway, LLMRequest, LLMUnavailable
from followup.llm.base import LLMError
from followup.llm.stub import StubClient
from followup.team.assistant import QuotesAnswer
from followup.team.check import ModelAudit
from followup.team.grading import GradeAnswer, Grader
from followup.team.quiz import Question, QuestionsAnswer, QuizMaker, issue
from followup.team.schema import CONTROL_STEPS, DEPARTMENT_STEPS
from followup.team.stub_answers import TEAM_KINDS
from followup.team.trace import TokensAnswer
from followup.team.trust import Ledger
from teamkit import team_settings

TODAY = date(2026, 10, 21)
SPEC = (
    "Install four handholes along Elm Street. "
    "The crew must record GPS coordinates for each handhole. "
    "Splice loss should stay under 0.5 dB with `--dry-run` enabled."
)


def memory(steps):
    db = sqlite3.connect(":memory:", isolation_level=None)
    for step in steps:
        db.executescript(step.sql)
    return db


def request(kind, **facts):
    return LLMRequest(kind, "system", "prompt", {}, facts)


def test_each_kind_validates_against_its_answer_type():
    gateway = LLMGateway(StubClient(), 20)
    quiz = gateway.ask(request("quiz_questions", spec=SPEC), QuestionsAnswer)
    assert quiz.questions and all(q.quote in SPEC for q in quiz.questions)
    tokens = gateway.ask(request("spec_tokens", spec=SPEC), TokensAnswer)
    assert "--dry-run" in tokens.tokens
    grade = {"answer": "under 0.5 dB", "key_terms": ["0.5"]}
    assert gateway.ask(request("grade_answer", **grade), GradeAnswer).correct
    assert gateway.ask(request("audit_grade", **grade), GradeAnswer).correct
    ask = request("assist_answer", spec=SPEC, question="What splice loss is allowed?")
    assert gateway.ask(ask, QuotesAnswer).quotes


def test_the_stub_grader_wants_the_terms_and_cites_the_answer():
    gateway = LLMGateway(StubClient(), 20)
    wrong = gateway.ask(request("grade_answer", answer="no idea", key_terms=["0.5"]), GradeAnswer)
    assert (wrong.correct, wrong.evidence) == (False, "")
    right = gateway.ask(
        request("grade_answer", answer="Stay UNDER 0.5 dB", key_terms=["0.5"]), GradeAnswer
    )
    assert right.correct and right.evidence == "0.5"


def test_stub_questions_survive_grounding(tmp_path):
    rules = team_settings(tmp_path, AUDIT_RATE="0")
    audit = ModelAudit(Ledger(memory(CONTROL_STEPS), "model_events"), "stub", rules, TODAY)
    maker = QuizMaker(LLMGateway(StubClient(), 20), memory(DEPARTMENT_STEPS), audit)
    found = maker.candidates("TC-1", SPEC, False)
    assert any(q.source == "model" for q in found)
    assert [e.passed for e in audit.ledger.events("stub:quiz_questions")] == [True, True]


def test_unknown_kind_still_fatal():
    assert "read_note" not in TEAM_KINDS
    with pytest.raises(LLMError) as raised:
        StubClient().complete(request("something_else"))
    assert raised.value.fatal
    with pytest.raises(LLMUnavailable):
        LLMGateway(StubClient(), 20).ask(request("something_else"), GradeAnswer)


def test_base_kinds_are_unchanged():
    completion = StubClient().complete(
        LLMRequest("read_note", "s", "p", {}, {"note": "Waiting for materials."})
    )
    assert json.loads(completion.text)["blocker"] == "materials"


def test_a_near_miss_splits_the_stub_from_the_rules_and_goes_to_review(tmp_path):
    rules = team_settings(tmp_path, AUDIT_RATE="0")
    audit = ModelAudit(Ledger(memory(CONTROL_STEPS), "model_events"), "stub", rules, TODAY)
    db = memory(DEPARTMENT_STEPS)
    gateway = LLMGateway(StubClient(), 20)
    question = Question(
        "q1", "Fill in", "Install four handholes.", ("install", "handholes"), "rules"
    )
    quiz = issue(
        db,
        task_id="TC-1",
        person="eve",
        issuer="max",
        spec="x",
        level="normal",
        questions=[question],
        day=TODAY,
    )
    grader = Grader(gateway, gateway, db, audit, 0.6)
    grade = grader.grade(quiz, question, "The crew installed the handholes")
    assert (grade.rule_pass, grade.model_pass, grade.verdict) == (False, True, "needs_review")
