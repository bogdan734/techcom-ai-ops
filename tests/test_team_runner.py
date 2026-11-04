import json
import sqlite3
from contextlib import closing
from datetime import date
from types import SimpleNamespace

import pytest

from conftest import no_wait_policy, raw_task
from followup.config import Settings
from followup.delivery import Delivery
from followup.llm import LLMGateway
from followup.llm.stub import StubClient
from followup.mock_api import MockBackend
from followup.models import Action, Decision
from followup.normalize import normalise_all
from followup.pipeline import run as base_run
from followup.store import Store
from followup.team import vault as vault_module
from followup.team.notices import Channel, Kind, Message
from followup.team.org import Org
from followup.team.runner import _deliver, team_run
from followup.team.schema import CONTROL, CONTROL_STEPS, DEPARTMENT_STEPS, Step
from followup.team.vault import VaultError
from teamkit import sample_people, team_settings

SPEC = (
    "Install four handholes along Elm Street. "
    "The crew must record GPS coordinates for each handhole. "
    "Splice loss should stay under 0.5 dB."
)


def records(eve="Working on it"):
    return [
        raw_task(
            task_id="TC-1",
            assigned_to="Eve Engineer",
            status=eve,
            spec=SPEC,
            assigned_by="max",
            due_date="2026-10-30",
            notes="On schedule.",
        ),
        raw_task(
            task_id="TC-2",
            assigned_to="Finn Field",
            status="Blocked",
            last_update="2026-10-15",
            notes="Awaiting materials from warehouse; delivery date unknown.",
            due_date="2026-10-18",
        ),
        raw_task(task_id="TC-3", assigned_to=None, status="Not Started"),
    ]


class Env:
    def __init__(self, tmp_path, people=None, **env):
        self.tmp = tmp_path
        self.out = tmp_path / "out"
        self.team = team_settings(tmp_path / "cfg", people=people, **{"AUDIT_RATE": "0", **env})

    def settings(self, today):
        return Settings.from_env({"OUT_DIR": str(self.out), "EVALUATION_DATE": today})

    def run(self, recs, today="2026-10-21", faults=None, team=None, client=None):
        settings = self.settings(today)
        backend = MockBackend(
            recs,
            self.out / "delivery_log.jsonl",
            faults=faults,
            call_log=self.out / "call_log.jsonl",
        )
        tasks, _ = normalise_all(recs)
        gateway = LLMGateway(client or StubClient(), 500)
        with backend.client() as http:
            return team_run(
                settings, team or self.team, recs, tasks, http, no_wait_policy(), gateway, gateway
            )

    def log(self):
        path = self.out / "delivery_log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_base_outcomes_equal_a_base_run(tmp_path):
    env = Env(tmp_path)
    result = env.run(records())
    fixture = tmp_path / "tasks.json"
    fixture.write_text(json.dumps(records()))
    plain = base_run(
        Settings.from_env(
            {
                "TASKS_FILE": str(fixture),
                "OUT_DIR": str(tmp_path / "plain"),
                "EVALUATION_DATE": "2026-10-21",
            }
        )
    )
    key = lambda o: (o.task_id, o.decision, o.target, o.action_id)  # noqa: E731
    assert [key(o) for o in result.outcomes] == [key(o) for o in plain.outcomes]
    assert any(o.delivery == "sent" for o in result.outcomes)


def test_team_mode_never_creates_state_db(tmp_path):
    env = Env(tmp_path)
    env.run(records())
    assert not (env.out / "state.db").exists()
    assert {p.name for p in (env.out / "state").glob("*.db")} == {
        "_control.db",
        "_unrouted.db",
        "engineering.db",
        "field-ops.db",
        "management.db",
        "support.db",
    }


def test_second_run_sends_nothing_team_json_identical(tmp_path):
    env = Env(tmp_path)
    first = env.run(records())
    log, artefact = env.log(), (env.out / "team.json").read_bytes()
    second = env.run(records())
    assert (first.replay, second.replay) == ("new", "replay")
    assert env.log() == log and (env.out / "team.json").read_bytes() == artefact
    assert second.artefact_digest == first.artefact_digest and second.undelivered == 0


def test_changed_artefact_same_input_is_mismatch(tmp_path):
    env = Env(tmp_path)
    env.run(records())
    with closing(sqlite3.connect(env.out / "state" / "_control.db")) as db:
        db.execute("UPDATE run_ledger SET artefact_digest = 'tampered'")
        db.commit()
    assert env.run(records()).replay == "mismatch"


def test_new_input_is_a_new_run_not_a_replay(tmp_path):
    env = Env(tmp_path)
    env.run(records())
    assert env.run(records(), today="2026-10-22").replay == "new"


def test_moving_a_person_resends_nothing(tmp_path):
    env = Env(tmp_path)
    env.run(records())
    sent = env.log()
    assert any(entry["to"] == "warehouse" for entry in sent)  # Finn's blocked task was followed up
    # Finn changes department; his task stays where it was first seen, with its sent record
    moved = [
        {**p, "department": "engineering"} if p["id"] == "finn" else p for p in sample_people()
    ]
    after = Env(tmp_path, people=moved)
    after.run(records())
    assert after.log() == sent

    # the provider would dedupe a resend by key; the point is that the local memory never moved
    def held(department):
        with closing(sqlite3.connect(env.out / "state" / f"{department}.db")) as db:
            return {row[0] for row in db.execute("SELECT task_id FROM actions")}

    assert "TC-2" in held("field-ops") and "TC-2" not in held("engineering")


def test_pending_migration_refuses_the_run(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.run(records())
    later = Step("003-later", "CREATE TABLE later (x INTEGER);")
    monkeypatch.setattr(
        vault_module,
        "steps_for",
        lambda name: CONTROL_STEPS if name == CONTROL else (*DEPARTMENT_STEPS, later),
    )
    with pytest.raises(VaultError, match="pending steps"):
        env.run(records())


def test_unsplit_legacy_state_refuses_the_run(tmp_path):
    env = Env(tmp_path)
    env.out.mkdir()
    store = Store(env.out / "state.db", date(2026, 10, 21))
    store.claim(Action("a1", "TC-1", Decision.FOLLOW_UP_OWNER, "x", "r", "m", "template"))
    store.close()
    with pytest.raises(VaultError, match="never split"):
        env.run(records())
    assert not (env.out / "state").exists()  # nothing was created beside the old state


def test_failed_notice_counts_undelivered(tmp_path):
    env = Env(tmp_path)
    env.run(records())
    sent = len(env.log())
    closed = records("Done")
    down = env.run(closed, today="2026-10-22", faults={"send": ["503"] * 80})
    assert down.undelivered > 0 and len(env.log()) == sent
    back = env.run(closed, today="2026-10-22")  # the provider is back: it all goes out, once
    assert back.undelivered == 0 and len(env.log()) > sent
    assert len({entry["idempotency_key"] for entry in env.log()}) == len(env.log())


def test_a_task_closing_asks_the_quiz_tells_the_issuer_and_names_the_next_task(tmp_path):
    env = Env(tmp_path)
    env.run(records())  # day 1: the layer starts, nothing is news
    start = len(env.log())
    day2 = records("Done")
    day2.append(
        raw_task(
            task_id="TC-4", assigned_to="Eve Engineer", due_date="2026-11-02", status="Not Started"
        )
    )
    env.run(day2, today="2026-10-22")
    new = env.log()[start:]
    by_to = {}
    for entry in new:
        by_to.setdefault(entry["to"], []).append(entry["body"])
    assert any("Completion check for TC-1" in body for body in by_to["Eve Engineer"])
    assert any("Next on your list: TC-4" in body for body in by_to["Eve Engineer"])
    assert any("closed TC-1" in body for body in by_to["Max Manager"])


def test_answers_in_the_department_file_become_a_verdict_next_run(tmp_path):
    env = Env(tmp_path)
    env.run(records())
    env.run(records("Done"), today="2026-10-22")
    with closing(sqlite3.connect(env.out / "state" / "engineering.db")) as db:
        quiz_id, questions = db.execute("SELECT quiz_id, questions FROM quizzes").fetchone()
        for question in json.loads(questions):
            if question["graded"]:
                db.execute(
                    "INSERT INTO answers VALUES (?, ?, ?, 0, '2026-10-22')",
                    (quiz_id, question["question_id"], " ".join(question["key_terms"])),
                )
        db.commit()
    before = len(env.log())
    env.run(records("Done"), today="2026-10-23")
    verdicts = [e for e in env.log()[before:] if "Completion check for TC-1" in e["body"]]
    assert [(e["to"], "passed" in e["body"]) for e in verdicts] == [("Max Manager", True)]
    with closing(sqlite3.connect(env.out / "state" / "_control.db")) as db:
        assert db.execute("SELECT passed FROM trust_events").fetchall() == [(1,)]


def test_the_artefact_carries_no_levels_or_scores(tmp_path):
    env = Env(tmp_path, TRUST_VISIBILITY="issuers")
    env.run(records())
    env.run(records("Done"), today="2026-10-22")
    text = (env.out / "team.json").read_text()
    for word in ("tightened", "reduced", "score", "trust"):
        assert word not in text


def test_a_reminder_never_overtakes_its_quiz(tmp_path):
    env = Env(tmp_path)
    env.run(records())
    closed = records("Done")
    env.run(closed, today="2026-10-22", faults={"send": ["503"] * 80})  # the quiz cannot go out
    env.run(closed, today="2026-10-23")  # a day later: the quiz and its reminder are both due
    bodies = [e["body"] for e in env.log() if e["to"] == "Eve Engineer"]
    quiz = next(i for i, b in enumerate(bodies) if b.startswith("Completion check for TC-1"))
    reminder = next(i for i, b in enumerate(bodies) if b.startswith("Reminder: the completion"))
    assert quiz < reminder


class _FakeStore:
    def __init__(self):
        self.sent = set()

    def get_action(self, message_id):
        return SimpleNamespace(status="sent") if message_id in self.sent else None


class _FakeOutbox:
    def __init__(self, store, failing=()):
        self.store, self.failing, self.delivered = store, set(failing), []

    def deliver(self, action):
        self.delivered.append(action.action_id)
        if action.action_id in self.failing:
            return Delivery("failed", action, detail="down")
        self.store.sent.add(action.action_id)
        return Delivery("sent", action, "m-1")


def _ladder_department(failing=()):
    quiz = Message(
        Kind.QUIZ,
        "TC-1",
        "eve",
        Channel.MESSAGE,
        "q",
        {"task_id": "TC-1", "title": "T", "assignee_id": "eve", "questions": "1. x"},
    )
    reminder = Message(
        Kind.QUIZ_REMINDER,
        "TC-1",
        "eve",
        Channel.MESSAGE,
        "q",
        {"task_id": "TC-1", "title": "T"},
        quiz.message_id,
    )
    store = _FakeStore()
    outbox = _FakeOutbox(store, {quiz.message_id} if failing else ())
    department = SimpleNamespace(
        messages=[reminder, quiz], store=store, outbox=outbox, calls=outbox, db=None
    )
    return department, quiz, reminder, outbox


def test_a_reminder_waits_until_its_quiz_was_sent():
    org = Org.from_records(sample_people())
    department, quiz, reminder, outbox = _ladder_department(failing=True)
    assert _deliver({"engineering": department}, org, None, date(2026, 10, 22)) == 1
    assert outbox.delivered == [quiz.message_id]  # the reminder was not even tried

    department, quiz, reminder, outbox = _ladder_department()
    assert _deliver({"engineering": department}, org, None, date(2026, 10, 22)) == 0
    assert outbox.delivered == [quiz.message_id, reminder.message_id]  # in order, in one pass
