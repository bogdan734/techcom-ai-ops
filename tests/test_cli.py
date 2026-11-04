from __future__ import annotations

import getpass
import json
from contextlib import closing

import pytest

from conftest import FIXTURE
from followup.cli import main
from followup.config import Settings
from followup.models import Action, Decision
from followup.store import Store


def test_run_writes_decisions_delivery_and_audit_logs(tmp_path, capsys):
    out = tmp_path / "out"

    code = main(["run", "--tasks", str(FIXTURE), "--today", "2026-10-21", "--out", str(out)])

    assert code == 0
    assert len(json.loads((out / "decisions.json").read_text())) == 10
    assert len((out / "delivery_log.jsonl").read_text().splitlines()) == 7
    assert (out / "audit.jsonl").exists()
    assert "delivery sent 7" in capsys.readouterr().out


def test_dry_run_flag_sends_nothing(tmp_path, capsys):
    out = tmp_path / "out"

    code = main(
        ["run", "--tasks", str(FIXTURE), "--today", "2026-10-21", "--out", str(out), "--dry-run"]
    )

    assert code == 0
    assert not (out / "delivery_log.jsonl").exists()
    assert "would_send 7" in capsys.readouterr().out


def test_invalid_date_is_a_configuration_error(tmp_path, capsys):
    code = main(["run", "--today", "21/10/2026", "--out", str(tmp_path)])

    assert code == 1
    assert "'21/10/2026' is not a YYYY-MM-DD date" in capsys.readouterr().err


def test_unknown_fault_is_a_configuration_error(tmp_path, capsys):
    code = main(["run", "--today", "2026-10-21", "--out", str(tmp_path), "--fault", "tasks=418"])

    assert code == 1
    assert "unknown fault '418' for tasks" in capsys.readouterr().err


def test_real_provider_requires_a_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    code = main(["run", "--provider", "anthropic", "--out", str(tmp_path)])

    assert code == 1
    assert "needs ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_missing_fixture_is_a_configuration_error(tmp_path, capsys):
    code = main(["run", "--tasks", str(tmp_path / "none.json"), "--out", str(tmp_path)])

    assert code == 1
    assert "cannot read tasks fixture" in capsys.readouterr().err


def action(action_id: str) -> Action:
    return Action(
        action_id=action_id,
        task_id="TC-1001",
        decision=Decision.FOLLOW_UP_OWNER,
        target="John Smith",
        reason="Overdue by 3 days",
        message="Hi John, TC-1001 is 3 days overdue. Please reply with the status.",
        drafted_by="template",
    )


def test_list_and_requeue_dead_actions(tmp_path, capsys, monkeypatch):
    out = tmp_path / "out"
    with closing(Store(out / "state.db")) as store:
        store.claim(action("act-dead"))
        store.claim(action("act-waiting"))
        store.start_attempt("act-dead")
        store.start_attempt("act-dead")
        store.mark_failed("act-dead", "HTTP 503")
        store.mark_dead("act-dead", "HTTP 400: bad recipient")
    monkeypatch.setattr(getpass, "getuser", lambda: "ops")

    assert main(["list", "dead", "--out", str(out)]) == 0
    assert capsys.readouterr().out == (
        f"act-dead TC-1001 {Decision.FOLLOW_UP_OWNER.value} -> John Smith | "
        "attempts 2, failed runs 1 | HTTP 400: bad recipient\n"
        "    Hi John, TC-1001 is 3 days overdue. Please reply with the status.\n"
    )

    assert main(["requeue", "act-dead", "--out", str(out)]) == 0
    events = [json.loads(line) for line in (out / "audit.jsonl").read_text().splitlines()]
    requeued = [event for event in events if event["event"] == "action_requeued"]
    assert [(event["action_id"], event["by"]) for event in requeued] == [("act-dead", "ops")]
    assert requeued[0]["run_id"]

    assert main(["requeue", "act-dead", "--out", str(out)]) == 1
    assert "act-dead is not a dead letter" in capsys.readouterr().err


def test_list_without_state_is_an_error_and_creates_nothing(tmp_path, capsys):
    out = tmp_path / "out"

    code = main(["list", "dead", "--out", str(out)])

    assert code == 1
    assert f"followup: no state at {out / 'state.db'}" in capsys.readouterr().err
    assert not out.exists()


def test_requeue_without_state_is_an_error_and_creates_nothing(tmp_path, capsys):
    out = tmp_path / "out"

    code = main(["requeue", "act-1", "--out", str(out)])

    assert code == 1
    assert f"followup: no state at {out / 'state.db'}" in capsys.readouterr().err
    assert not out.exists()  # not even the audit log: nothing was requeued


def test_state_commands_ignore_unrelated_bad_settings(tmp_path, monkeypatch):
    out = tmp_path / "out"
    with closing(Store(out / "state.db")):
        pass
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert main(["list", "dead", "--out", str(out)]) == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("TRUE", True), ("1", True), ("False", False), ("0", False), ("", False)],
)
def test_approval_flag_accepts_true_false_1_and_0_in_any_case(raw, expected):
    assert Settings.from_env({"APPROVE_ESCALATIONS": raw}).approve_escalations is expected


def test_invalid_approval_flag_is_a_configuration_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("APPROVE_ESCALATIONS", "maybe")

    code = main(["run", "--tasks", str(FIXTURE), "--today", "2026-10-21", "--out", str(tmp_path)])

    assert code == 1
    assert "APPROVE_ESCALATIONS='maybe'" in capsys.readouterr().err


def test_approve_releases_a_waiting_escalation(tmp_path, capsys, monkeypatch):
    out = tmp_path / "out"
    with closing(Store(out / "state.db")) as store:
        store.claim(action("act-waiting"), "awaiting_approval")
    monkeypatch.setattr(getpass, "getuser", lambda: "ops")
    # Like list and requeue, approve needs the out directory and nothing else.
    monkeypatch.setenv("APPROVE_ESCALATIONS", "maybe")

    assert main(["list", "awaiting_approval", "--out", str(out)]) == 0
    listed = capsys.readouterr().out
    assert "act-waiting" in listed and action("act-waiting").message in listed

    assert main(["approve", "act-waiting", "--out", str(out)]) == 0
    with closing(Store(out / "state.db")) as store:
        assert store.get_action("act-waiting").status == "pending"
    events = [json.loads(line) for line in (out / "audit.jsonl").read_text().splitlines()]
    approved = [event for event in events if event["event"] == "action_approved"]
    assert [(event["action_id"], event["by"]) for event in approved] == [("act-waiting", "ops")]

    assert main(["approve", "act-waiting", "--out", str(out)]) == 1
    assert "act-waiting is not waiting for approval" in capsys.readouterr().err


def test_list_shows_a_dash_for_an_action_without_an_error(tmp_path, capsys):
    out = tmp_path / "out"
    with closing(Store(out / "state.db")) as store:
        store.claim(action("act-waiting"), "awaiting_approval")

    assert main(["list", "awaiting_approval", "--out", str(out)]) == 0
    assert "attempts 0, failed runs 0 | -\n" in capsys.readouterr().out
