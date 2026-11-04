"""Five days on the demo fixtures, through the CLI and the run."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from followup.cli import main
from followup.config import Settings
from followup.pipeline import run
from followup.run_lock import hold_run_lock
from followup.team.calibrate import load_gold
from followup.team.extras import load_extras
from followup.team.org import Org
from followup.team.spec_terms import identifiers
from followup.team.trace import trace

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures" / "team"
ENV = {"CALLS": "true", "CALL_TIME": "10:00", "AUDIT_RATE": "0"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("ORG_FILE", "TASKS_FILE", "OUT_DIR", "EVALUATION_DATE", "TRUST_VISIBILITY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)


class Demo:
    def __init__(self, out):
        self.out = out

    def day(self, date, tasks="tasks-d2.json", faults=None):
        settings = Settings.from_env(
            {
                "ORG_FILE": str(FIX / "org.json"),
                "TASKS_FILE": str(FIX / tasks),
                "OUT_DIR": str(self.out),
                "EVALUATION_DATE": date,
                **ENV,
            }
        )
        return run(settings, faults)

    def team(self, date, *args):
        return main(
            [
                "team",
                *args,
                "--org",
                str(FIX / "org.json"),
                "--tasks",
                str(FIX / "tasks-d2.json"),
                "--out",
                str(self.out),
                "--today",
                date,
            ]
        )

    def log(self):
        return [
            json.loads(line) for line in (self.out / "delivery_log.jsonl").read_text().splitlines()
        ]

    def to(self, name):
        return [e["body"] for e in self.log() if e["to"] == name]


def test_five_days_of_the_team_layer(tmp_path, capsys):
    demo = Demo(tmp_path / "out")

    # day 1: the layer starts; old work is not announced, a task created yesterday is
    first = demo.day("2026-10-21", "tasks-d1.json")
    assert first.exit_code == 0 and first.team.replay == "new"
    assert any("New task TC-1007" in body for body in demo.to("Taylor Reed"))
    assert not any("closed TC-100" in e["body"] for e in demo.log())

    # day 2: two tasks close, a task appears, a spec changes
    demo.day("2026-10-22")
    assert any("Completion check for TC-1001" in b for b in demo.to("John Smith"))
    assert any("Completion check for TC-1005" in b for b in demo.to("Alex Brown"))
    assert any("John Smith closed TC-1001" in b for b in demo.to("Mia Lindqvist"))
    assert any("Alex Brown closed TC-1005" in b for b in demo.to("Noah Becker"))
    assert any("no other open tasks" in b for b in demo.to("John Smith"))
    assert any("New task TC-1011" in b for b in demo.to("Maria Cruz"))
    changed = [b for b in demo.to("Taylor Reed") if b.startswith("The spec of TC-1007")]
    assert changed and "Traffic control plans" in changed[0]
    assert any(b.startswith("The spec of TC-1007") for b in demo.to("Mia Lindqvist"))

    # people act; nothing is sent until the next run
    before = len(demo.log())
    assert demo.team("2026-10-22", "answer", "TC-1005", "--as", "alex", "--all", "not sure") == 0
    assert (
        demo.team(
            "2026-10-22", "chat", "TC-1011", "Which photos does the audit list?", "--as", "maria"
        )
        == 0
    )
    assert demo.team("2026-10-22", "support", "I cannot open the quiz page", "--as", "alex") == 0
    assert len(demo.log()) == before

    # day 3: a verdict, an assistant answer, a ticket, a reminder
    demo.day("2026-10-23")
    verdict = [b for b in demo.to("Noah Becker") if b.startswith("Completion check for TC-1005")]
    assert verdict and "failed" in verdict[0] and "--layer-map" in verdict[0]
    assert any(
        'From the task text: "The audit should list unreadable photos by ID."' in b
        for b in demo.to("Maria Cruz")
    )
    assert not any(
        "wrote on TC-1011" in b for b in demo.to("Mia Lindqvist")
    )  # the spec answered it
    assert all(
        any("Support ticket from Alex Brown" in b for b in demo.to(agent))
        for agent in ("Sue Park", "Tom Alvarez")
    )
    assert any(
        b.startswith("Reminder: the completion check for TC-1001") for b in demo.to("John Smith")
    )

    # day 4: the unanswered check becomes a call; nobody picks up
    demo.day("2026-10-24", faults={"call": ["no-answer"]})
    calls = [json.loads(line) for line in (demo.out / "call_log.jsonl").read_text().splitlines()]
    assert [(c["to"], c["answered_by"]) for c in calls] == [("+15551110001", "none")]

    # day 5: the issuer hears it; then the same day again changes nothing
    demo.day("2026-10-25")
    assert any("not answered by a person (no answer)" in b for b in demo.to("Mia Lindqvist"))
    lines = len(demo.log())
    again = demo.day("2026-10-25")
    assert again.team.replay == "replay" and again.exit_code == 0 and len(demo.log()) == lines

    keys = [e["idempotency_key"] for e in demo.log()]
    assert len(keys) == len(set(keys))
    assert not (demo.out / "state.db").exists()
    text = (demo.out / "team.json").read_text()
    assert not any(word in text for word in ("tightened", "reduced", "halted", "score"))


def test_the_base_decisions_are_those_of_a_run_without_the_team_layer(tmp_path):
    demo = Demo(tmp_path / "team")
    team_run = demo.day("2026-10-21", "tasks-d1.json")
    plain = run(
        Settings.from_env(
            {
                "TASKS_FILE": str(FIX / "tasks-d1.json"),
                "OUT_DIR": str(tmp_path / "plain"),
                "EVALUATION_DATE": "2026-10-21",
            }
        )
    )
    key = lambda o: (o.task_id, o.decision, o.target, o.action_id)  # noqa: E731
    assert [key(o) for o in team_run.outcomes] == [key(o) for o in plain.outcomes]


def spawn(tmp_path, out, day, cwd):
    env = {**os.environ, **ENV, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "followup",
            "run",
            "--org",
            str(FIX / "org.json"),
            "--tasks",
            str(FIX / "tasks-d2.json"),
            "--out",
            str(out),
            "--today",
            day,
        ],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_a_second_process_while_one_holds_the_lock_exits_75(tmp_path):
    out = tmp_path / "out"
    with hold_run_lock(out):
        process = spawn(tmp_path, out, "2026-10-22", tmp_path)
        _, err = process.communicate(timeout=60)
    assert process.returncode == 75 and "another run" in err
    assert not (out / "delivery_log.jsonl").exists()


def test_two_processes_started_together_never_send_twice(tmp_path):
    out = tmp_path / "out"
    first, second = (
        spawn(tmp_path, out, "2026-10-22", tmp_path),
        spawn(tmp_path, out, "2026-10-22", tmp_path),
    )
    codes = sorted([first.wait(timeout=120), second.wait(timeout=120)])
    assert codes[0] == 0 and codes[1] in (0, 75)
    third = spawn(tmp_path, out, "2026-10-22", tmp_path)
    assert third.wait(timeout=120) == 0
    keys = [
        json.loads(line)["idempotency_key"]
        for line in (out / "delivery_log.jsonl").read_text().splitlines()
    ]
    assert keys and len(keys) == len(set(keys))


def test_the_demo_fixtures_agree_with_each_other():
    org = Org.load(FIX / "org.json")
    for name in ("tasks-d1.json", "tasks-d2.json"):
        records = json.loads((FIX / name).read_text())
        extras = load_extras(records, org)  # an issuer that cannot issue would raise here
        owners = {r["assigned_to"] for r in records if r.get("assigned_to")}
        assert all(org.by_name(owner) for owner in owners)
        assert all(extra.spec for extra in extras.values())
    scenario = json.loads((FIX / "scenario.json").read_text())
    assert [d["date"] for d in scenario["days"]] == sorted({d["date"] for d in scenario["days"]})
    assert all((FIX / d["tasks"]).exists() for d in scenario["days"])


def test_the_code_check_finds_one_name_and_misses_the_other():
    org = Org.load(FIX / "org.json")
    spec = load_extras(json.loads((FIX / "tasks-d2.json").read_text()), org)["TC-1005"].spec
    found = {t.token: t.found_in for t in trace(identifiers(spec), FIX, "code/asbuilt")}
    assert found["--export-dxf"] and found["route_summary"] and not found["--layer-map"]


def test_the_gold_set_has_both_classes_and_calibrate_scores_it(tmp_path, capsys):
    assert len({item.label for item in load_gold(FIX / "gold.json")}) == 2
    demo = Demo(tmp_path / "out")
    demo.day("2026-10-21", "tasks-d1.json")
    assert demo.team("2026-10-21", "calibrate", "--gold", str(FIX / "gold.json")) == 0
    assert "rules   kappa 1.00" in capsys.readouterr().out
    assert json.loads((demo.out / "calibration.json").read_text())["n"] == 10
