import json
import sqlite3
from contextlib import closing

import pytest

from followup.team import tasks as tasks_module
from followup.team.actions import (
    accept_task,
    create_task,
    return_task,
    submit_task,
)
from followup.team.context import ActionError
from followup.team.dashboard import build_dashboard
from followup.team.schema import CONTROL
from teamkit import SPEC, run_day, world_context, world_records

NEW = {"title": "Label the splice closures", "spec": SPEC, "project": "SURF Illinois"}


def person(ctx, pid):
    return ctx.org.person(pid)


def board(ctx, pid):
    return build_dashboard(ctx, person(ctx, pid))


def started(tmp_path, status="Working on it", today="2026-10-21", more=(), **env):
    run_day(tmp_path, world_records(status, more), today, **env)
    return world_context(tmp_path, world_records(status, more), today, **env)


def bodies(tmp_path, name):
    path = tmp_path / "out" / "delivery_log.jsonl"
    return [e["body"] for e in map(json.loads, path.read_text().splitlines()) if e["to"] == name]


def test_effective_records_apply_the_latest_move_and_the_authored_tasks(tmp_path):
    ctx = started(tmp_path)
    records = world_records()
    assert tasks_module.effective_records(ctx.vault, records)[0]["status"] == "Working on it"
    db = ctx.vault.open(CONTROL)
    for kind, day in (("submitted", "2026-10-22"), ("returned", "2026-10-23")):
        db.execute(
            "INSERT INTO task_events (task_id, kind, person_id, day) VALUES ('tc-1', ?, 'eve', ?)",
            (kind, day),
        )
    db.execute(
        "INSERT INTO task_events (task_id, kind, person_id, day) "
        "VALUES ('TC-1', 'submitted', 'eve', '2026-10-24')"
    )
    merged = tasks_module.effective_records(ctx.vault, records)
    assert merged[0]["status"] == "Done" and merged[0]["last_update"] == "2026-10-24"
    assert merged[1] == records[1]  # a task nobody moved is untouched
    assert records[0]["status"] == "Working on it"  # the input is not changed


def test_an_issuer_gives_a_task_and_the_assignee_hears_on_the_next_run(tmp_path):
    ctx = started(tmp_path)
    task_id = create_task(ctx, person(ctx, "max"), assignee_id="eve", **NEW)
    assert task_id == "APP-0001"
    assert create_task(ctx, person(ctx, "max"), assignee_id="finn", **NEW) == "APP-0002"
    run_day(tmp_path, world_records(), "2026-10-21")
    assert any(
        b.startswith("New task APP-0001: Label the splice closures")
        for b in bodies(tmp_path, "Eve Engineer")
    )
    ctx = world_context(tmp_path, world_records(), "2026-10-21")
    assert [c.task_id for c in board(ctx, "eve").mine] == ["APP-0001", "TC-1"]
    assert board(ctx, "max").assignable and {p["id"] for p in board(ctx, "max").assignable} == {
        "eve",
        "finn",
    }


def test_only_an_issuer_gives_tasks_and_only_to_their_own_line(tmp_path):
    ctx = started(tmp_path)
    with pytest.raises(ActionError, match="may not give tasks"):
        create_task(ctx, person(ctx, "eve"), assignee_id="finn", **NEW)
    with pytest.raises(ActionError, match="own line"):
        create_task(ctx, person(ctx, "max"), assignee_id="sue", **NEW)
    with pytest.raises(ActionError, match="own line"):
        create_task(ctx, person(ctx, "max"), assignee_id="sid", **NEW)
    assert board(ctx, "eve").assignable == ()


def test_a_task_needs_a_title_a_spec_and_a_real_date(tmp_path):
    ctx = started(tmp_path)
    for bad, match in (
        ({"title": ""}, "needs a title"),
        ({"spec": "too short"}, "needs a title"),
        ({"due": "next week"}, "YYYY-MM-DD"),
    ):
        with pytest.raises(ActionError, match=match):
            create_task(ctx, person(ctx, "max"), assignee_id="eve", **{**NEW, **bad})


def test_the_assignee_accepts_hands_in_and_the_issuer_sees_each_step(tmp_path):
    ctx = started(tmp_path, "Not Started")
    mine = board(ctx, "eve").mine[0]
    assert mine.can_accept and mine.can_submit and not mine.can_return
    accept_task(ctx, person(ctx, "eve"), "TC-1")
    ctx = world_context(tmp_path, world_records("Not Started"), "2026-10-22")
    assert (
        board(ctx, "eve").mine[0].task_status == "Working on it"
        and not board(ctx, "eve").mine[0].can_accept
    )
    run_day(tmp_path, world_records("Not Started"), "2026-10-22")
    assert any(
        b == "Eve Engineer started TC-1 (Fiber splicing)." for b in bodies(tmp_path, "Max Manager")
    )
    submit_task(ctx, person(ctx, "eve"), "TC-1", "all done")
    run_day(tmp_path, world_records("Not Started"), "2026-10-23")
    assert any("Eve Engineer closed TC-1" in b for b in bodies(tmp_path, "Max Manager"))
    assert any(b.startswith("Completion check for TC-1") for b in bodies(tmp_path, "Eve Engineer"))


def test_nobody_else_can_accept_or_hand_in_and_nothing_is_done_twice(tmp_path):
    ctx = started(tmp_path, "Not Started")
    for who in ("finn", "max"):
        with pytest.raises(ActionError, match="only the assignee"):
            accept_task(ctx, person(ctx, who), "TC-1")
        with pytest.raises(ActionError, match="only the assignee"):
            submit_task(ctx, person(ctx, who), "TC-1")
    accept_task(ctx, person(ctx, "eve"), "TC-1")
    ctx = world_context(tmp_path, world_records("Not Started"), "2026-10-21")
    with pytest.raises(ActionError, match="already under way"):
        accept_task(ctx, person(ctx, "eve"), "TC-1")
    submit_task(ctx, person(ctx, "eve"), "TC-1")
    ctx = world_context(tmp_path, world_records("Not Started"), "2026-10-21")
    with pytest.raises(ActionError, match="already finished"):
        submit_task(ctx, person(ctx, "eve"), "TC-1")


def test_the_issuer_sends_finished_work_back_with_a_reason(tmp_path):
    ctx = started(tmp_path)
    run_day(tmp_path, world_records("Done"), "2026-10-22")
    ctx = world_context(tmp_path, world_records("Done"), "2026-10-22")
    assert board(ctx, "max").issued[0].can_return and not board(ctx, "eve").mine[0].can_return
    with pytest.raises(ActionError, match="only the person who issued"):
        return_task(ctx, person(ctx, "sid"), "TC-1", "redo it")
    with pytest.raises(ActionError, match="what has to change"):
        return_task(ctx, person(ctx, "max"), "TC-1", "")
    return_task(ctx, person(ctx, "max"), "TC-1", "photos are missing")
    ctx = world_context(tmp_path, world_records("Done"), "2026-10-22")
    assert board(ctx, "eve").mine[0].task_status == "Working on it"
    run_day(tmp_path, world_records("Done"), "2026-10-23")
    assert any(
        "Sent back for rework: photos are missing" in b for b in bodies(tmp_path, "Eve Engineer")
    )


def test_only_finished_work_can_be_sent_back(tmp_path):
    ctx = started(tmp_path)
    with pytest.raises(ActionError, match="only a finished task"):
        return_task(ctx, person(ctx, "max"), "TC-1", "not yet")


def test_accounts_and_moves_live_in_the_control_file_only(tmp_path):
    started(tmp_path)
    with closing(sqlite3.connect(tmp_path / "out" / "state" / "engineering.db")) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not names & {"accounts", "sessions", "task_events", "authored_tasks"}
