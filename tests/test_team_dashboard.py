import pytest

from followup.team.actions import submit_answers
from followup.team.context import ActionError
from followup.team.dashboard import build_dashboard, explain, to_markdown
from teamkit import run_day, world_context, world_records

ISSUERS = {"TRUST_VISIBILITY": "issuers"}


def started(tmp_path, **env):
    run_day(tmp_path, world_records(), **env)
    run_day(tmp_path, world_records("Done"), "2026-10-22", **env)
    return world_context(tmp_path, world_records("Done"), "2026-10-22", **env)


def board(ctx, pid):
    return build_dashboard(ctx, ctx.org.person(pid))


def test_the_assignee_sees_the_quiz_and_can_answer_it(tmp_path):
    ctx = started(tmp_path)
    (mine,) = board(ctx, "eve").mine
    assert (mine.task_id, mine.status, mine.can_answer, mine.can_review) == (
        "TC-1",
        "open",
        True,
        False,
    )
    assert (
        mine.questions[0].prompt.startswith("In your own words")
        and mine.questions[0].answer is None
    )
    assert board(ctx, "eve").issued == ()


def test_the_issuer_sees_the_task_but_cannot_answer_it(tmp_path):
    ctx = started(tmp_path)
    ids = [c.task_id for c in board(ctx, "max").issued]
    assert ids == ["TC-1", "TC-2"]  # his line: Eve and Finn
    assert not any(c.can_answer for c in board(ctx, "max").issued)
    assert [c.task_id for c in board(ctx, "finn").mine] == ["TC-2"]


def test_no_viewer_sees_their_own_score(tmp_path):
    ctx = started(tmp_path, **ISSUERS)
    for pid in ("max", "sid", "bea"):
        assert pid not in {t.person_id for t in board(ctx, pid).trust}
    assert board(ctx, "eve").trust is None and board(ctx, "finn").trust is None


def test_manager_sees_only_their_line(tmp_path):
    ctx = started(tmp_path, **ISSUERS)
    assert {t.person_id for t in board(ctx, "max").trust} == {"eve", "finn"}
    assert {t.person_id for t in board(ctx, "sid").trust} == {"max", "eve", "finn", "sue"}
    assert board(ctx, "sue").trust is None  # support has no line and no right to see trust


def test_hidden_has_no_trust_section_even_for_boss(tmp_path):
    ctx = started(tmp_path)  # hidden is the default
    assert all(board(ctx, pid).trust is None for pid in ("bea", "sid", "max"))
    with pytest.raises(PermissionError, match="hidden"):
        explain(ctx, ctx.org.person("bea"), "eve")


def test_explain_refuses_self_outsiders_and_non_issuers(tmp_path):
    ctx = started(tmp_path, **ISSUERS)
    person = ctx.org.person
    with pytest.raises(PermissionError):
        explain(ctx, person("max"), "max")
    with pytest.raises(PermissionError):
        explain(ctx, person("max"), "sue")  # support is outside Max's line
    with pytest.raises(PermissionError):
        explain(ctx, person("eve"), "finn")
    assert explain(ctx, person("sid"), "eve") == []


def test_notices_are_the_messages_sent_to_the_viewer(tmp_path):
    ctx = started(tmp_path)
    eve = board(ctx, "eve").notices
    assert any(n.body.startswith("Completion check for TC-1") for n in eve)
    assert all(n.channel == "message" for n in eve)
    assert any("closed TC-1" in n.body for n in board(ctx, "max").notices)
    assert not any("Completion check" in n.body for n in board(ctx, "finn").notices)


def test_models_section_is_for_issuers_only(tmp_path):
    ctx = started(tmp_path)
    assert board(ctx, "eve").models is None and board(ctx, "max").models is not None


def test_the_report_is_stable_markdown(tmp_path):
    ctx = started(tmp_path, **ISSUERS)
    first = to_markdown(board(ctx, "max"))
    assert first == to_markdown(board(ctx, "max"))
    assert "## Check levels in my line" in first and "Eve Engineer" in first
    assert "Eve Engineer (engineer): normal" in first
    assert "Check levels" not in to_markdown(board(ctx, "eve"))


def test_an_answered_check_waits_for_the_next_run_then_shows_its_verdict(tmp_path):
    ctx = started(tmp_path)
    quiz = board(ctx, "eve").mine[0]
    answers = {q.number: " ".join(q.prompt.split()[:3]) for q in quiz.questions if q.graded}
    submit_answers(ctx, ctx.org.person("eve"), "TC-1", answers)
    assert board(ctx, "eve").mine[0].status == "answered"
    run_day(tmp_path, world_records("Done"), "2026-10-23")
    ctx = world_context(tmp_path, world_records("Done"), "2026-10-23")
    assert board(ctx, "eve").mine[0].status in {"pass", "fail", "needs_review"}
    assert board(ctx, "max").issued[0].questions[0].answer is not None
    with pytest.raises(ActionError):
        ctx.person("bogus")
