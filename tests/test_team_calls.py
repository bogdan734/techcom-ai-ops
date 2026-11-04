import json
import sqlite3
from datetime import date, timedelta

import httpx2
import pytest

from conftest import no_wait_policy
from followup.config import ConfigError
from followup.delivery import Outbox
from followup.faults import FaultSpecError, parse_faults
from followup.mock_api import MockBackend
from followup.store import Store
from followup.team.calls import (
    AnsweredBy,
    CallsApi,
    can_call,
    counts_as_delivered,
    ladder,
    pending_calls,
    provider_client,
    record_result,
    results,
    settle_result,
)
from followup.team.notices import Channel, Kind, Message, to_action
from followup.team.schema import DEPARTMENT_STEPS
from teamkit import sample_people, team_settings

D0 = date(2026, 10, 21)
BODY = {"to": "+15550000001", "task_id": "TC-1", "decision": "notify", "body": "Please answer."}


def backend(tmp_path, faults=None, log=True):
    return MockBackend([], faults=faults, call_log=tmp_path / "calls.jsonl" if log else None)


def post(mock, key, body=BODY):
    client = mock.client()
    return client.post("/calls", json=body, headers={"Idempotency-Key": key})


def call_action():
    message = Message(
        Kind.QUIZ_CALL,
        "TC-1",
        "eve",
        Channel.CALL,
        "k",
        {"task_id": "TC-1", "assignee": "Eve", "spoken": ""},
    )
    return to_action(message, "+15550000001")


def test_same_key_places_one_call(tmp_path):
    mock = backend(tmp_path)
    first, second = post(mock, "k1"), post(mock, "k1")
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["message_id"] == second.json()["message_id"] == "call-0001"
    assert len((tmp_path / "calls.jsonl").read_text().splitlines()) == 1
    assert post(mock, "k2").json()["message_id"] == "call-0002"


def test_a_call_needs_a_key_and_a_body(tmp_path):
    mock = backend(tmp_path)
    assert mock.client().post("/calls", json=BODY).status_code == 400
    assert post(mock, "k", {"to": "x"}).status_code == 422


def test_calls_are_looked_up_by_key(tmp_path):
    mock = backend(tmp_path)
    post(mock, "k1")
    client = mock.client()
    assert client.get("/calls/k1").json()["answered_by"] == "person"
    assert client.get("/calls/none").status_code == 404


def test_machine_and_no_answer_are_outcomes_not_errors(tmp_path):
    mock = backend(tmp_path, faults={"call": ["machine", "no-answer"]})
    assert post(mock, "a").json()["answered_by"] == "machine"
    assert post(mock, "b").json()["answered_by"] == "none"
    assert post(mock, "c").json()["answered_by"] == "person"


def test_timeout_after_commit_is_reconciled_not_redialled(tmp_path):
    mock = backend(tmp_path, faults={"call": ["timeout-after-commit"]})
    store = Store(tmp_path / "s.db")
    outbox = Outbox(store, CallsApi(mock.client(), no_wait_policy()), no_wait_policy())
    delivery = outbox.deliver(call_action())
    assert delivery.status == "reconciled"
    assert len((tmp_path / "calls.jsonl").read_text().splitlines()) == 1
    assert len(mock.call_records) == 1
    assert outbox.deliver(call_action()).status == "duplicate_suppressed"
    assert [c for c in mock.calls if c.startswith("POST /calls")] == ["POST /calls -> 201, lost"]


def test_calls_do_not_touch_the_message_route(tmp_path):
    mock = backend(tmp_path)
    store = Store(tmp_path / "s.db")
    Outbox(store, CallsApi(mock.client(), no_wait_policy()), no_wait_policy()).deliver(
        call_action()
    )
    assert mock.messages == {} and all("/messages" not in c for c in mock.calls)


def test_base_backend_writes_no_call_log(tmp_path):
    mock = backend(tmp_path, log=False)
    assert post(mock, "k").status_code == 201
    assert list(tmp_path.glob("*.jsonl")) == []


def test_a_restarted_backend_remembers_its_calls(tmp_path):
    post(backend(tmp_path), "k1")
    again = backend(tmp_path)
    assert (
        post(again, "k1").status_code == 200
        and json.loads(post(again, "k1").text)["message_id"] == "call-0001"
    )


def test_no_consent_or_outside_window_no_call_rung(tmp_path):
    people = sample_people()
    eve = next(p for p in people if p["id"] == "eve")
    finn = next(p for p in people if p["id"] == "finn")  # a phone, but no consent
    on = team_settings(tmp_path, CALLS="true", CALL_TIME="10:00")
    late = team_settings(tmp_path, CALLS="true", CALL_TIME="22:30")
    off = team_settings(tmp_path, CALLS="false", CALL_TIME="10:00")
    org = on.org
    assert can_call(org.person(eve["id"]), on) is True
    assert can_call(org.person(finn["id"]), on) is False
    assert can_call(org.person(eve["id"]), late) is False
    assert can_call(org.person(eve["id"]), off) is False
    nothing = ladder(
        D0, D0 + timedelta(days=5), 1, can_call(org.person("eve"), late), "verification"
    )
    assert Channel.CALL not in {r.channel for r in nothing}


def test_call_rung_waits_two_steps():
    def kinds(days):
        return [r.kind for r in ladder(D0, D0 + timedelta(days=days), 1, True, "verification")]

    assert kinds(0) == [Kind.QUIZ]
    assert kinds(1) == [Kind.QUIZ, Kind.QUIZ_REMINDER]
    assert kinds(2) == [Kind.QUIZ, Kind.QUIZ_REMINDER, Kind.QUIZ_CALL]
    assert [r.kind for r in ladder(D0, D0 + timedelta(days=3), 2, True, "verification")] == [
        Kind.QUIZ,
        Kind.QUIZ_REMINDER,
    ]


def test_machine_counts_for_reminder_not_verification():
    assert counts_as_delivered("reminder", AnsweredBy.MACHINE)
    assert not counts_as_delivered("verification", AnsweredBy.MACHINE)
    assert counts_as_delivered("verification", AnsweredBy.PERSON)
    assert not counts_as_delivered("reminder", AnsweredBy.NONE)


def test_results_round_trip_and_are_first_writer_wins():
    db = sqlite3.connect(":memory:", isolation_level=None)
    for step in DEPARTMENT_STEPS:
        db.executescript(step.sql)
    record_result(db, "k1", "eve", "TC-1", AnsweredBy.MACHINE, "reminder", D0)
    record_result(db, "k1", "eve", "TC-1", AnsweredBy.PERSON, "reminder", D0)
    assert results(db) == {"k1": (AnsweredBy.MACHINE, "reminder")}


def test_cli_accepts_call_machine_fault():
    assert parse_faults(["call=machine,no-answerx2", "call_lookup=503"]) == {
        "call": ["machine", "no-answer", "no-answer"],
        "call_lookup": ["503"],
    }
    with pytest.raises(FaultSpecError):
        parse_faults(["send=machine"])
    assert httpx2  # the mock speaks httpx2, as the service does


def test_a_real_provider_needs_https_unless_it_is_this_computer(tmp_path):
    base = {"CALLS": "true", "CALLS_PROVIDER": "http"}
    for bad in ("", "http://voice.example", "ftp://voice.example"):
        with pytest.raises(ConfigError, match="CALLS_API_URL"):
            team_settings(tmp_path, **base, CALLS_API_URL=bad)
    assert (
        team_settings(tmp_path, **base, CALLS_API_URL="https://voice.example").calls_provider
        == "http"
    )
    assert (
        team_settings(tmp_path, **base, CALLS_API_URL="http://127.0.0.1:9000").calls_provider
        == "http"
    )


def test_unknown_provider_purpose_or_a_short_secret_are_refused(tmp_path):
    with pytest.raises(ConfigError, match="CALLS_PROVIDER"):
        team_settings(tmp_path, CALLS_PROVIDER="carrier-pigeon")
    with pytest.raises(ConfigError, match="CALL_PURPOSE"):
        team_settings(tmp_path, CALL_PURPOSE="chat")
    with pytest.raises(ConfigError, match="CALLS_WEBHOOK_SECRET"):
        team_settings(tmp_path, CALLS_WEBHOOK_SECRET="short")


def test_secrets_stay_out_of_repr_and_out_of_the_decision_digest(tmp_path):
    settings = team_settings(
        tmp_path, CALLS_API_KEY="key-123456", CALLS_WEBHOOK_SECRET="webhook-secret-123456"
    )
    text = repr(settings) + repr(settings.decision_fields())
    assert "key-123456" not in text and "webhook-secret-123456" not in text


def test_provider_client_is_only_built_for_a_real_provider(tmp_path):
    assert provider_client(team_settings(tmp_path)) is None
    assert provider_client(team_settings(tmp_path, CALLS="true")) is None  # the mock
    real = team_settings(
        tmp_path,
        CALLS="true",
        CALLS_PROVIDER="http",
        CALLS_API_URL="https://voice.example",
        CALLS_API_KEY="k-1",
    )
    client = provider_client(real)
    assert str(client.base_url).startswith("https://voice.example")
    assert client.headers["Authorization"] == "Bearer k-1"
    client.close()
    off = team_settings(tmp_path, CALLS="false", CALLS_PROVIDER="http")
    assert provider_client(off) is None


def test_extras_ride_along_with_the_call_and_nothing_else_changes(tmp_path):
    mock = backend(tmp_path)
    api = CallsApi(
        mock.client(),
        no_wait_policy(),
        {"k9": {"purpose": "verification", "questions": [{"number": 1}]}},
    )
    assert api.post("k9", BODY).json()["purpose"] == "verification"
    assert "purpose" not in api.post("k10", BODY).json()


def test_a_pending_result_settles_once_and_never_changes_again():
    db = sqlite3.connect(":memory:", isolation_level=None)
    for step in DEPARTMENT_STEPS:
        db.executescript(step.sql)
    record_result(db, "k1", "eve", "TC-1", AnsweredBy.PENDING, "verification", D0)
    assert pending_calls(db) == ["k1"]
    assert settle_result(db, "k1", AnsweredBy.PENDING) is False
    assert settle_result(db, "k1", AnsweredBy.MACHINE) is True
    assert settle_result(db, "k1", AnsweredBy.PERSON) is False
    assert results(db) == {"k1": (AnsweredBy.MACHINE, "verification")} and pending_calls(db) == []
