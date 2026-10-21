from __future__ import annotations

from datetime import date

import pytest

from conftest import make_task, raw_task
from followup.models import Status
from followup.normalize import InvalidRecord, normalise_all, normalise_task


def test_fixture_normalises_without_issues_except_missing_fields(fixture_records):
    tasks, rejected = normalise_all(fixture_records)

    assert rejected == []
    assert [t.task_id for t in tasks] == [f"TC-{n}" for n in range(1001, 1011)]
    by_id = {t.task_id: t for t in tasks}
    assert by_id["TC-1004"].owner is None
    assert by_id["TC-1005"].due_date is None
    assert by_id["TC-1003"].status is Status.DONE
    assert all(t.issues == () for t in tasks)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Working on it", Status.IN_PROGRESS),
        ("  WORKING_ON_IT ", Status.IN_PROGRESS),
        ("in progress", Status.IN_PROGRESS),
        ("Not Started", Status.NOT_STARTED),
        ("blocked", Status.BLOCKED),
        ("Completed", Status.DONE),
    ],
)
def test_status_variants(raw, expected):
    assert make_task(status=raw).status is expected


def test_unknown_status_is_kept_raw_and_reported():
    task = make_task(status="On hold")

    assert task.status is Status.UNKNOWN
    assert task.status_raw == "On hold"
    assert task.issues == ("status 'On hold' is not recognised",)


@pytest.mark.parametrize("value", [None, "", "  ", "Unassigned", "N/A", "tbd"])
def test_placeholder_owners_mean_no_owner(value):
    assert make_task(assigned_to=value).owner is None


def test_owner_whitespace_is_collapsed():
    assert make_task(assigned_to="  Maria   Cruz ").owner == "Maria Cruz"


def test_iso_dates_and_utc_timestamps():
    task = make_task(due_date="2026/10/18", last_update="2026-10-20T23:30:00-05:00")

    assert task.due_date == date(2026, 10, 18)
    assert task.last_update == date(2026, 10, 21)  # converted to UTC before taking the date


@pytest.mark.parametrize("value", ["10/11/2026", "next Friday", 20261018])
def test_ambiguous_or_invalid_dates_are_not_guessed(value):
    task = make_task(due_date=value)

    assert task.due_date is None
    assert task.issues == (f"due_date {value!r} is not an ISO date",)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(" tc-1001 ", "TC-1001"), ("Task  7", "TASK 7"), ("ab#3", "AB#3"), (1001, "1001")],
)
def test_task_id_is_trimmed_and_uppercased_but_otherwise_kept(raw, expected):
    assert make_task(task_id=raw).task_id == expected


@pytest.mark.parametrize("value", [None, "", "   ", True, {"id": 1}, "X" * 65])
def test_record_without_usable_id_is_rejected(value):
    with pytest.raises(InvalidRecord):
        normalise_task(raw_task(task_id=value))


def test_long_titles_are_bounded_because_they_reach_prompts_and_messages():
    task = make_task(task="T" * 500, project="P" * 300)

    assert (len(task.title), len(task.project)) == (200, 200)
    assert task.issues == (
        "project truncated to 200 characters",
        "task truncated to 200 characters",
    )


def test_notes_are_cleaned_and_bounded():
    task = make_task(notes="line one\n\tline\x00two " + "x" * 2000)

    assert task.notes.startswith("line one line two ")
    assert len(task.notes) == 1000
    assert task.issues == ("notes truncated to 1000 characters",)


def test_batch_reports_bad_records_and_keeps_newest_duplicate():
    records = [
        raw_task(task_id="TC-1", last_update="2026-10-18", notes="old"),
        "not an object",
        raw_task(task_id="TC-1", last_update="2026-10-20", notes="new"),
        raw_task(task_id=None),
    ]

    tasks, rejected = normalise_all(records)

    assert [(t.task_id, t.notes) for t in tasks] == [("TC-1", "new")]
    assert rejected == [
        "record #1: expected an object, got str",
        "record #2: duplicate TC-1, kept the copy last updated 2026-10-20",
        "record #3: task_id missing or not a string: None",
    ]
