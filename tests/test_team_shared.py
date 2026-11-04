import pytest

from conftest import make_task, raw_task
from followup.config import ConfigError
from followup.models import Status
from followup.team import tasks as tasks_module
from followup.team.actions import create_task, return_task, submit_task
from followup.team.context import ActionError
from followup.team.dashboard import build_dashboard, to_markdown
from followup.team.extras import TaskExtras, load_extras
from followup.team.org import Org
from followup.team.shared import shared_tasks
from teamkit import SPEC, run_day, sample_people, world_context, world_records

NEW = {"title": "Label the splice closures", "spec": SPEC, "project": "SURF Illinois"}


def person(ctx, pid):
    return ctx.org.person(pid)


def board(ctx, pid):
    return build_dashboard(ctx, person(ctx, pid))


def started(tmp_path):
    run_day(tmp_path, world_records(), "2026-10-21")
    return world_context(tmp_path, world_records(), "2026-10-21")


def reload(tmp_path, today="2026-10-21"):
    return world_context(tmp_path, world_records(), today)


def bodies(tmp_path, name):
    import json

    path = tmp_path / "out" / "delivery_log.jsonl"
    return [e["body"] for e in map(json.loads, path.read_text().splitlines()) if e["to"] == name]


def whole(ctx, viewer, task_id):
    """How the task as a whole stands, as the viewer's board shows it."""
    shown = board(ctx, viewer)
    return next(c for c in [*shown.mine, *shown.issued] if c.task_id == task_id).shared


def give(ctx, who=("eve", "finn"), **more):
    return create_task(ctx, person(ctx, "max"), assignee_ids=list(who), **{**NEW, **more})


def group_of(*statuses):
    tasks = [make_task(task_id=f"APP-000{n}", status=s) for n, s in enumerate(statuses, start=1)]
    extras = {t.task_id: TaskExtras(t.task_id, SPEC, "max", None, None, "APP-0001") for t in tasks}
    return shared_tasks(tasks, extras)["APP-0001"]


def test_a_shared_task_is_done_only_when_every_part_is():
    assert group_of("Not Started", "Not Started").status is Status.NOT_STARTED
    assert group_of("Done", "Not Started").status is Status.IN_PROGRESS
    assert group_of("Done", "Working on it").status is Status.IN_PROGRESS
    assert group_of("Blocked", "Not Started").status is Status.IN_PROGRESS
    assert group_of("Done", "Done", "Done").status is Status.DONE
    assert group_of("Done", "Not Started", "Done").done == 2


def test_a_group_of_one_and_a_task_without_a_group_are_not_shared():
    alone, plain = make_task(task_id="APP-0001"), make_task(task_id="APP-0002")
    extras = {"APP-0001": TaskExtras("APP-0001", SPEC, "max", None, None, "APP-0001")}
    assert shared_tasks([alone, plain], extras) == {}


def test_a_group_in_the_records_is_a_task_id():
    org = Org.from_records(sample_people())
    record = raw_task(task_id="APP-0002", spec=SPEC, assigned_by="max", group=" app-0001 ")
    assert load_extras([record], org)["APP-0002"].group == "APP-0001"
    with pytest.raises(ConfigError, match="group must be"):
        load_extras([{**record, "group": 7}], org)


def test_a_task_given_to_two_people_is_a_part_each_under_one_group(tmp_path):
    ctx = started(tmp_path)
    assert give(ctx) == ["APP-0001", "APP-0002"]
    parts = tasks_module.authored_records(ctx.vault)
    assert [(r["task_id"], r["assigned_to"], r.get("group")) for r in parts] == [
        ("APP-0001", "Eve Engineer", "APP-0001"),
        ("APP-0002", "Finn Field", "APP-0001"),
    ]


def test_a_task_for_one_person_has_no_group_even_when_the_name_is_repeated(tmp_path):
    ctx = started(tmp_path)
    assert give(ctx, ("eve", "eve")) == ["APP-0001"]
    assert "group" not in tasks_module.authored_records(ctx.vault)[0]


def test_one_name_outside_the_line_refuses_the_whole_task(tmp_path):
    ctx = started(tmp_path)
    with pytest.raises(ActionError, match="own line"):
        give(ctx, ("eve", "sue"))
    with pytest.raises(ActionError, match="choose who gets the task"):
        give(ctx, ())
    assert tasks_module.authored_records(ctx.vault) == []


def test_the_shared_task_stays_in_progress_until_every_part_is_handed_in(tmp_path):
    ctx = started(tmp_path)
    give(ctx)
    run_day(tmp_path, world_records(), "2026-10-21")
    ctx = reload(tmp_path)
    start = whole(ctx, "max", "APP-0001")
    assert (start["status"], start["done"], start["total"]) == ("Not Started", 0, 2)

    submit_task(ctx, person(ctx, "eve"), "APP-0001")
    ctx = reload(tmp_path)
    half = whole(ctx, "max", "APP-0001")
    assert (half["status"], half["done"]) == ("Working on it", 1)
    assert [p["status"] for p in half["parts"]] == ["Done", "Not Started"]

    submit_task(ctx, person(ctx, "finn"), "APP-0002")
    ctx = reload(tmp_path)
    assert whole(ctx, "max", "APP-0001")["status"] == "Done"

    return_task(ctx, person(ctx, "max"), "APP-0002", "photos are missing")
    ctx = reload(tmp_path)
    again = whole(ctx, "max", "APP-0001")
    assert (again["status"], again["done"]) == ("Working on it", 1)


def test_each_assignee_sees_the_whole_task_and_the_report_says_how_far_it_is(tmp_path):
    ctx = started(tmp_path)
    give(ctx)
    run_day(tmp_path, world_records(), "2026-10-21")
    ctx = reload(tmp_path)
    mine = whole(ctx, "eve", "APP-0001")
    assert [p["assignee"] for p in mine["parts"]] == ["Eve Engineer", "Finn Field"]
    assert whole(ctx, "eve", "TC-1") is None
    assert "shared: 0 of 2 handed in" in to_markdown(board(ctx, "max"))


def test_each_person_hears_who_shares_the_task_and_the_issuer_how_it_stands(tmp_path):
    ctx = started(tmp_path)
    give(ctx)
    give(ctx, ("eve",), title="Solo job")
    run_day(tmp_path, world_records(), "2026-10-21")
    news = [b for b in bodies(tmp_path, "Eve Engineer") if b.startswith("New task")]
    assert any("APP-0001" in b and "Shared with Finn Field." in b for b in news)
    assert any("APP-0003" in b and "Shared with" not in b for b in news)
    assert any("Shared with Eve Engineer." in b for b in bodies(tmp_path, "Finn Field"))

    ctx = reload(tmp_path)
    submit_task(ctx, person(ctx, "eve"), "APP-0001")
    submit_task(ctx, person(ctx, "eve"), "APP-0003")
    run_day(tmp_path, world_records(), "2026-10-22")
    told = bodies(tmp_path, "Max Manager")
    assert any(
        "Eve Engineer closed APP-0001" in b
        and "1 of 2 have handed in, so the task stays in progress." in b
        for b in told
    )
    assert any("closed APP-0003" in b and "handed in" not in b for b in told)

    ctx = reload(tmp_path, "2026-10-22")
    submit_task(ctx, person(ctx, "finn"), "APP-0002")
    run_day(tmp_path, world_records(), "2026-10-23")
    assert any(
        "Finn Field closed APP-0002" in b and "All 2 have handed in, so the task is done." in b
        for b in bodies(tmp_path, "Max Manager")
    )


def test_the_overdue_follow_up_goes_to_the_part_that_is_still_open(tmp_path):
    ctx = started(tmp_path)
    give(ctx, due="2026-10-20")
    ctx = reload(tmp_path)
    submit_task(ctx, person(ctx, "eve"), "APP-0001")
    report = run_day(tmp_path, world_records(), "2026-10-21")
    decided = {o.task_id: (o.decision, o.target) for o in report.outcomes}
    assert decided["APP-0001"] == ("no_action", None)
    assert decided["APP-0002"] == ("follow_up_owner", "Finn Field")


def test_each_part_lives_in_the_department_of_its_own_person(tmp_path):
    ctx = started(tmp_path)
    give(ctx)
    run_day(tmp_path, world_records(), "2026-10-21")
    ctx = reload(tmp_path)
    assert [ctx.department(ctx.task(t)) for t in ("APP-0001", "APP-0002")] == [
        "engineering",
        "field-ops",
    ]
