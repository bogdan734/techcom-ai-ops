from __future__ import annotations

import json

from conftest import FIXTURE
from followup.cli import main


def test_run_writes_decisions_delivery_and_audit_logs(tmp_path, capsys):
    out = tmp_path / "out"

    code = main(["run", "--tasks", str(FIXTURE), "--today", "2026-10-21", "--out", str(out)])

    assert code == 0
    assert len(json.loads((out / "decisions.json").read_text())) == 10
    assert len((out / "delivery_log.jsonl").read_text().splitlines()) == 7
    assert (out / "audit.jsonl").exists()
    assert "delivery sent 7" in capsys.readouterr().out


def test_invalid_date_is_a_configuration_error(tmp_path, capsys):
    code = main(["run", "--today", "21/10/2026", "--out", str(tmp_path)])

    assert code == 1
    assert "'21/10/2026' is not a YYYY-MM-DD date" in capsys.readouterr().err


def test_unknown_fault_is_a_configuration_error(tmp_path, capsys):
    code = main(["run", "--today", "2026-10-21", "--out", str(tmp_path), "--fault", "tasks=418"])

    assert code == 1
    assert "unknown fault '418'" in capsys.readouterr().err


def test_real_provider_requires_a_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    code = main(["run", "--provider", "anthropic", "--out", str(tmp_path)])

    assert code == 1
    assert "needs ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_missing_fixture_is_a_configuration_error(tmp_path, capsys):
    code = main(["run", "--tasks", str(tmp_path / "none.json"), "--out", str(tmp_path)])

    assert code == 1
    assert "cannot read tasks fixture" in capsys.readouterr().err
