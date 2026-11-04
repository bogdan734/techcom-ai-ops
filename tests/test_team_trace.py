import sqlite3
from datetime import date

import pytest

from followup.llm import LLMGateway
from followup.llm.base import LLMError
from followup.team.check import ModelAudit
from followup.team.schema import CONTROL_STEPS, DEPARTMENT_STEPS
from followup.team.trace import TraceError, propose_tokens, python_names, trace
from followup.team.trust import Ledger
from teamkit import KindLLM, team_settings

TODAY = date(2026, 10, 21)
SPEC = "Add `--dry-run` and a /messages route. Log with splice_log. Keep retry_count low."


def tree(tmp_path, files):
    for name, content in files.items():
        path = tmp_path / "code" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return tmp_path / "code"


def found(root, token, path="."):
    (result,) = trace([token], root, path)
    return result


def test_argparse_flag_found(tmp_path):
    root = tree(
        tmp_path,
        {
            "cli.py": "import argparse\np = argparse.ArgumentParser()\n"
            "p.add_argument('--dry-run', action='store_true')\n"
        },
    )
    assert found(root, "--dry-run").found_in == ("cli.py:3",)


def test_token_only_in_a_comment_is_missing(tmp_path):
    root = tree(tmp_path, {"a.py": '"""retry_count lives here."""\n# retry_count later\nx = 1\n'})
    assert found(root, "retry_count").missing


def test_definitions_calls_and_attributes_count(tmp_path):
    root = tree(
        tmp_path, {"a.py": "def splice_log():\n    pass\nobj.retry_count = 3\nsend('/messages')\n"}
    )
    assert not found(root, "splice_log").missing
    assert not found(root, "retry_count").missing
    assert found(root, "/messages").found_in == ("a.py:4",)


def test_ship_does_not_match_shipment(tmp_path):
    root = tree(
        tmp_path,
        {"a.py": "def shipment():\n    return 'shipment'\n", "b.js": "var shipment = 1;\n"},
    )
    assert found(root, "ship").missing
    assert not found(root, "shipment").missing


def test_other_languages_get_a_word_search(tmp_path):
    root = tree(tmp_path, {"run.sh": "echo start\nfollowup --dry-run\n"})
    assert found(root, "--dry-run").found_in == ("run.sh:2",)


def test_code_path_outside_root_refused(tmp_path):
    root = tree(tmp_path, {"a.py": "x = 1\n"})
    (tmp_path / "secret.py").write_text("retry_count = 1\n")
    for bad in ("..", "../secret.py", "/etc"):
        with pytest.raises(TraceError, match="leaves the code root"):
            trace(["x"], root, bad)


def test_a_symlink_out_of_the_root_is_not_followed(tmp_path):
    root = tree(tmp_path, {"a.py": "x = 1\n"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.py").write_text("retry_count = 1\n")
    (root / "link").symlink_to(outside)
    with pytest.raises(TraceError):
        trace(["retry_count"], root, "link")
    assert found(root, "retry_count").missing


def test_a_missing_path_and_a_syntax_error_are_trace_errors(tmp_path):
    root = tree(tmp_path, {"broken.py": "def (:\n"})
    with pytest.raises(TraceError, match="does not exist"):
        trace(["x"], root, "nowhere")
    with pytest.raises(TraceError, match="does not parse"):
        trace(["x"], root, ".")


def test_python_names_skip_docstrings_and_long_strings():
    names = python_names(
        '"""doc retry_count"""\nx = "' + "y" * 300 + '"\ndef f():\n    """inner"""\n'
    )
    assert "doc retry_count" not in names and "inner" not in names
    assert "f" in names and ("y" * 300) not in names


def memory(steps):
    db = sqlite3.connect(":memory:", isolation_level=None)
    for step in steps:
        db.executescript(step.sql)
    return db


@pytest.fixture
def parts(tmp_path):
    rules = team_settings(tmp_path)
    audit = ModelAudit(Ledger(memory(CONTROL_STEPS), "model_events"), "fake", rules, TODAY)
    return memory(DEPARTMENT_STEPS), audit


def propose(parts, llm, spec=SPEC, flagged=False):
    db, audit = parts
    return propose_tokens("TC-1", spec, LLMGateway(llm, max_calls=20), db, audit, flagged)


def test_ungrounded_model_token_dropped(parts):
    llm = KindLLM({"spec_tokens": ['{"tokens": ["retry_count", "invented_function"]}']})
    tokens = propose(parts, llm)
    assert "invented_function" not in tokens
    assert tokens[:4] == ["--dry-run", "/messages", "splice_log", "retry_count"]
    (event,) = parts[1].ledger.events("fake:spec_tokens")
    assert event.passed is False  # the model invented a name, and the ledger remembers


def test_model_tokens_follow_the_rule_tokens_without_duplicates(parts):
    llm = KindLLM({"spec_tokens": ['{"tokens": ["low", "retry_count"]}']})
    assert propose(parts, llm) == ["--dry-run", "/messages", "splice_log", "retry_count", "low"]


def test_proposals_are_pinned_so_a_rerun_asks_nothing(parts):
    llm = KindLLM({"spec_tokens": ['{"tokens": ["low"]}']})
    first = propose(parts, llm)
    assert propose(parts, llm) == first and len(llm.requests["spec_tokens"]) == 1


def test_flagged_spec_never_reaches_the_model(parts):
    llm = KindLLM()
    assert propose(parts, llm, flagged=True) == [
        "--dry-run",
        "/messages",
        "splice_log",
        "retry_count",
    ]
    assert not llm.requests


def test_unavailable_model_leaves_the_rule_tokens(parts):
    llm = KindLLM({"spec_tokens": [LLMError("down", fatal=True)]})
    assert propose(parts, llm)[0] == "--dry-run"


def test_a_model_that_dropped_to_rules_is_not_asked(parts):
    _, audit = parts
    for number in range(4):
        audit.record("spec_tokens", f"i{number}", "grounding", False)
    llm = KindLLM()
    propose(parts, llm)
    assert not llm.requests
