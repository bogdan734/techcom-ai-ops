import json
import re
import shutil

import pytest

from followup.cli import main
from followup.run_lock import hold_run_lock
from teamkit import run_day, world_records


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env from the repository
    for name in ("ORG_FILE", "TASKS_FILE", "OUT_DIR", "EVALUATION_DATE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def state(tmp_path):
    run_day(tmp_path, world_records())
    run_day(tmp_path, world_records("Done"), "2026-10-22")
    return tmp_path


def team(tmp_path, *args, today="2026-10-22", org=True):
    extra = ["--org", str(tmp_path / "cfg" / "org.json")] if org else []
    return main(
        [
            "team",
            *args,
            *extra,
            "--tasks",
            str(tmp_path / "tasks.json"),
            "--out",
            str(tmp_path / "out"),
            "--today",
            today,
        ]
    )


def log(tmp_path):
    return (tmp_path / "out" / "delivery_log.jsonl").read_text().splitlines()


def test_quiz_shows_the_questions_and_how_to_answer(state, capsys):
    assert team(state, "quiz", "TC-1", "--as", "eve") == 0
    out = capsys.readouterr().out
    assert "1. In your own words" in out and "followup team answer TC-1 --as eve --answer 1=" in out


def answer_args(tmp_path, capsys, who="eve"):
    """An answer for every scored question, numbered the way the quiz prints them."""
    team(tmp_path, "quiz", "TC-1", "--as", who)
    lines = capsys.readouterr().out.splitlines()
    scored = [re.match(r"\s*(\d+)\. ", line) for line in lines if "(not scored)" not in line]
    return [item for m in scored if m for item in ("--answer", f"{m.group(1)}=answer {m.group(1)}")]


def test_answer_writes_the_rows_and_sends_nothing(state, capsys):
    before = log(state)
    pairs = answer_args(state, capsys)
    assert team(state, "answer", "TC-1", "--as", "eve", *pairs) == 0
    assert "recorded" in capsys.readouterr().out
    assert log(state) == before


def test_answer_twice_exits_1(state, capsys):
    pairs = answer_args(state, capsys)
    assert team(state, "answer", "TC-1", "--as", "eve", *pairs) == 0
    capsys.readouterr()
    assert team(state, "answer", "TC-1", "--as", "eve", *pairs) == 1
    assert "already answered" in capsys.readouterr().err


def test_answer_needs_a_number_and_a_text(state, capsys):
    assert team(state, "answer", "TC-1", "--as", "eve", "--answer", "one=text") == 1
    assert "write --answer N=TEXT" in capsys.readouterr().err
    assert team(state, "answer", "TC-1", "--as", "eve", "--answer", "1") == 1


def test_unknown_or_mailbox_identity_exits_1(state, capsys):
    assert team(state, "quiz", "TC-1", "--as", "nobody") == 1
    assert "unknown person" in capsys.readouterr().err
    assert team(state, "quiz", "TC-1", "--as", "warehouse") == 1


def test_engineer_trust_is_denied(state, capsys):
    assert team(state, "trust", "--as", "eve") == 1
    assert "hidden, or not yours" in capsys.readouterr().err
    assert team(state, "trust", "--as", "bea") == 1  # hidden by default, even for the boss


def test_busy_run_exits_75(state, capsys):
    pairs = answer_args(state, capsys)
    with hold_run_lock(state / "out"):
        assert team(state, "answer", "TC-1", "--as", "eve", *pairs) == 75
        assert team(state, "report", "--as", "eve") == 0  # reading does not need the lock
    assert "try again" in capsys.readouterr().err


def test_a_run_with_the_lock_taken_exits_75(state, capsys):
    with hold_run_lock(state / "out"):
        code = main(
            [
                "run",
                "--org",
                str(state / "cfg" / "org.json"),
                "--tasks",
                str(state / "tasks.json"),
                "--out",
                str(state / "out"),
                "--today",
                "2026-10-23",
            ]
        )
    assert code == 75 and "another run" in capsys.readouterr().err


def test_review_by_a_non_issuer_exits_1(state, capsys):
    assert team(state, "review", "TC-1", "--as", "eve", "--accept") == 1
    assert "may not review" in capsys.readouterr().err


def test_review_needs_accept_or_reject(state):
    with pytest.raises(SystemExit):
        team(state, "review", "TC-1", "--as", "max")


def test_team_without_an_org_is_a_config_error(state, capsys):
    assert team(state, "report", "--as", "eve", org=False) == 1
    assert "no org file" in capsys.readouterr().err


def test_chat_writes_then_reads_the_thread_with_the_assistants_answer(state, capsys):
    assert team(state, "chat", "TC-1", "What splice loss is allowed?", "--as", "eve") == 0
    assert (
        main(
            [
                "run",
                "--org",
                str(state / "cfg" / "org.json"),
                "--tasks",
                str(state / "tasks.json"),
                "--out",
                str(state / "out"),
                "--today",
                "2026-10-23",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert team(state, "chat", "TC-1", "--as", "eve", today="2026-10-23") == 0
    out = capsys.readouterr().out
    assert "Eve Engineer: What splice loss is allowed?" in out
    assert 'Assistant: From the task text: "Splice loss should stay under 0.5 dB."' in out


def test_chat_with_nobody_to_talk_to_exits_1(state, capsys):
    assert team(state, "chat", "TC-1", "hello", "--as", "finn") == 1
    assert "only the assignee and the issuer" in capsys.readouterr().err


def test_support_opens_lists_and_lets_support_reply(state, capsys):
    assert team(state, "support", "The quiz page does not open", "--as", "eve") == 0
    capsys.readouterr()
    assert team(state, "support", "--as", "eve") == 0
    line = capsys.readouterr().out.strip()
    assert "The quiz page does not open (waiting)" in line
    thread = line.split()[0]
    assert team(state, "reply", thread, "Try again now.", "--as", "sue") == 0
    assert team(state, "reply", thread, "hi", "--as", "finn") == 1


def test_report_is_text_or_json(state, capsys):
    assert team(state, "report", "--as", "max") == 0
    assert capsys.readouterr().out.startswith("# Max Manager (manager)")
    assert team(state, "report", "--as", "max", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["viewer"]["id"] == "max" and [c["task_id"] for c in data["issued"]] == [
        "TC-1",
        "TC-2",
    ]


def test_trust_for_an_issuer_when_levels_are_shown(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("TRUST_VISIBILITY", "issuers")
    run_day(tmp_path, world_records())
    assert team(tmp_path, "trust", "--as", "max", today="2026-10-21") == 0
    assert "Eve Engineer" in capsys.readouterr().out
    assert team(tmp_path, "trust", "--as", "max", "--person", "eve", today="2026-10-21") == 0
    assert team(tmp_path, "trust", "--as", "max", "--person", "max", today="2026-10-21") == 1


def test_run_with_an_org_flag_uses_department_files(tmp_path):
    shutil.rmtree(tmp_path / "out", ignore_errors=True)
    run_day(tmp_path, world_records())
    assert (tmp_path / "out" / "state" / "engineering.db").exists()
    assert not (tmp_path / "out" / "state.db").exists()


def test_answer_all_gives_one_answer_to_every_scored_question(state, capsys):
    before = log(state)
    assert team(state, "answer", "TC-1", "--as", "eve", "--all", "under 0.5 dB") == 0
    assert "recorded" in capsys.readouterr().out and log(state) == before
    assert team(state, "answer", "TC-1", "--as", "eve", "--all", "again") == 1


def test_answer_all_and_pairs_together_are_refused(state, capsys):
    assert team(state, "answer", "TC-1", "--as", "eve", "--answer", "1=x", "--all", "y") == 1
    assert "either --all" in capsys.readouterr().err
    assert team(state, "answer", "TC-1", "--as", "eve") == 1


def test_users_add_list_remove_and_a_short_password(state, capsys, monkeypatch):
    import io

    def users(*args, password=None):
        if password is not None:
            monkeypatch.setattr("sys.stdin", io.StringIO(password + "\n"))
        return team(state, "users", *args, today="2026-10-22")

    assert users("add", "eve", "--password-stdin", password="short") == 1
    assert "at least 8" in capsys.readouterr().err
    assert users("add", "eve", "--password-stdin", password="a long enough one") == 0
    assert users("add", "warehouse", "--password-stdin", password="a long enough one") == 1
    capsys.readouterr()
    assert users("list") == 0 and "eve" in capsys.readouterr().out
    assert users("remove", "eve") == 0
    assert users("remove", "eve") == 1
    assert users("add", "--password-stdin", password="x") == 1


def test_users_demo_creates_an_account_for_everyone_who_can_act(state, capsys):
    assert team(state, "users", "demo", today="2026-10-22") == 0
    out = capsys.readouterr().out
    assert (
        "eve          demo-eve-pass" in out and "warehouse" not in out and "public by design" in out
    )
