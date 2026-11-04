import http.client
import json
import sqlite3
import threading
from contextlib import closing

import pytest

from followup.run_lock import hold_run_lock
from followup.team.webapp import AppState, make_server, prepare
from teamkit import SPEC, world_records, world_settings

SECRET = "s3cret-for-webhooks-123"


def scenario(tmp_path, dates=("2026-10-21", "2026-10-22", "2026-10-23")):
    days = []
    for n, date in enumerate(dates, start=1):
        name = f"day{n}.json"
        (tmp_path / "scenario").mkdir(exist_ok=True)
        (tmp_path / "scenario" / name).write_text(
            json.dumps(world_records("Done" if n > 1 else "Working on it"))
        )
        days.append(
            {
                "date": date,
                "tasks": name,
                "label": f"Day {n}",
                "title": f"t{n}",
                "note": "english",
                "note_ru": "по-русски",
            }
        )
    path = tmp_path / "scenario" / "scenario.json"
    path.write_text(json.dumps({"title": "test", "days": days}))
    return path


class Client:
    """One browser: its own cookie, talking to one server."""

    def __init__(self, server):
        self.server, self.cookie = server, ""

    def call(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=30)
        sent = {"Content-Type": "application/json", "X-Followup": "1", **(headers or {})}
        if self.cookie:
            sent["Cookie"] = self.cookie
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.server.server_port}")
        payload = json.dumps(body).encode() if body is not None else None
        for name, value in sent.items():
            conn.putheader(name, value)
        conn.putheader("Content-Length", str(len(payload or b"")))
        conn.endheaders(payload)
        response = conn.getresponse()
        raw = response.read()
        self.set_cookie = response.getheader("Set-Cookie")
        conn.close()
        try:
            return response.status, json.loads(raw)
        except ValueError:
            return response.status, raw.decode()

    def login(self, person, password=None):
        status, body = self.call(
            "POST", "/api/login", {"person": person, "password": password or f"demo-{person}-pass"}
        )
        if status == 200:
            self.cookie = self.set_cookie.split(";")[0]
        return status, body


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("ORG_FILE", "TASKS_FILE", "OUT_DIR", "EVALUATION_DATE", "CALLS_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    settings = world_settings(tmp_path, world_records(), CALLS_WEBHOOK_SECRET=SECRET)
    shown = prepare(settings, demo=True)
    state = AppState(settings, scenario(tmp_path), shown)
    server = make_server(state)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield type(
        "App",
        (),
        {
            "server": server,
            "state": state,
            "tmp": tmp_path,
            "client": lambda self, who=None: _signed(server, who),
        },
    )()
    server.shutdown()
    server.server_close()


def _signed(server, who):
    client = Client(server)
    if who:
        assert client.login(who)[0] == 200
    return client


def play(app, days):
    manager = app.client("max")
    for _ in range(days):
        assert manager.call("POST", "/api/advance", {})[0] == 200


def test_the_page_is_served_without_signing_in(app):
    status, text = Client(app.server).call("GET", "/")
    assert status == 200 and "<title>Follow-up Desk</title>" in text


def test_nothing_is_readable_before_signing_in(app):
    anonymous = Client(app.server)
    assert anonymous.call("GET", "/api/session") == (200, {"person": None})
    for path in ("/api/me", "/api/state"):
        assert anonymous.call("GET", path)[0] in (401, 404)
    assert anonymous.call("POST", "/api/answer", {"task": "TC-1", "answers": {}})[0] == 401
    assert anonymous.call("POST", "/api/advance", {})[0] == 401


def test_signing_in_needs_the_right_password_and_sets_a_strict_cookie(app):
    client = Client(app.server)
    assert client.login("eve", "wrong password")[0] == 403
    assert client.login("nobody", "demo-nobody-pass")[0] == 403
    status, body = client.login("eve")
    assert status == 200 and body["person"]["name"] == "Eve Engineer"
    assert "HttpOnly" in client.set_cookie and "SameSite=Strict" in client.set_cookie
    assert client.call("GET", "/api/session")[1]["person"]["id"] == "eve"


def test_five_wrong_passwords_lock_the_account_for_a_while(app):
    client = Client(app.server)
    for _ in range(5):
        assert client.login("finn", "wrong password")[0] == 403
    assert client.login("finn")[0] == 403  # even the right one, until the lock ends


def test_a_person_cannot_act_as_somebody_else_by_naming_them(app):
    play(app, 2)
    eve = app.client("eve")
    board = eve.call("GET", "/api/me?as=max")[1]
    assert board["viewer"]["id"] == "eve"  # the name in the request is ignored
    status, error = eve.call("POST", "/api/review", {"as": "max", "task": "TC-1", "accept": True})
    assert status == 400 and "may not review" in error["error"]
    answers = {"1": "x"}
    assert (
        eve.call("POST", "/api/answer", {"as": "finn", "task": "TC-1", "answers": answers})[0]
        == 400
    )


def test_signing_out_ends_the_session(app):
    eve = app.client("eve")
    assert eve.call("POST", "/api/logout", {})[0] == 200 and "Max-Age=0" in eve.set_cookie
    assert eve.call("GET", "/api/me")[0] == 401


def test_demo_logins_are_listed_only_when_the_app_was_started_with_them(app, tmp_path):
    shown = Client(app.server).call("GET", "/api/demo")[1]
    assert {"id": "eve", "password": "demo-eve-pass"} in shown
    quiet = make_server(AppState(app.state.base, None, {}))
    threading.Thread(target=quiet.serve_forever, daemon=True).start()
    assert Client(quiet).call("GET", "/api/demo")[1] == []
    quiet.shutdown()
    quiet.server_close()


def test_only_a_manager_moves_the_days_along(app):
    assert app.client("eve").call("POST", "/api/advance", {})[0] == 403
    play(app, 3)
    state = (
        app.client("eve").call("GET", "/api/state")[1]
        if False
        else app.client("eve").call("GET", "/api/session")[1]["scenario"]
    )
    assert state["done"] == 3 and state["days"][0]["note_ru"] == "по-русски"
    status, error = app.client("max").call("POST", "/api/advance", {})
    assert status == 400 and "no more days" in error["error"]


def test_the_dashboard_is_each_persons_own(app):
    play(app, 2)
    eve = app.client("eve").call("GET", "/api/me")[1]
    assert eve["mine"][0]["can_answer"] and eve["trust"] is None and eve["assignable"] == []
    max_ = app.client("max").call("GET", "/api/me")[1]
    assert {p["id"] for p in max_["assignable"]} == {"eve", "finn"}
    assert app.client("finn").call("GET", "/api/me")[1]["issued"] == []


def test_changes_need_the_app_header_json_and_a_local_origin(app):
    eve = app.client("eve")
    assert eve.call("POST", "/api/ticket", {"text": "x"}, headers={"X-Followup": "0"})[0] == 403
    assert (
        eve.call("POST", "/api/ticket", {"text": "x"}, headers={"Content-Type": "text/plain"})[0]
        == 403
    )
    assert (
        eve.call("POST", "/api/ticket", {"text": "x"}, headers={"Origin": "http://evil.example"})[0]
        == 403
    )
    assert eve.call("GET", "/api/session", host="evil.example")[0] == 403
    assert eve.call("GET", "/api/unknown")[0] == 404


def test_answer_chat_done_support_over_http(app):
    play(app, 2)
    eve = app.client("eve")
    board = eve.call("GET", "/api/me")[1]
    answers = {
        str(q["number"]): "under 0.5 dB" for q in board["mine"][0]["questions"] if q["graded"]
    }
    assert eve.call("POST", "/api/answer", {"task": "TC-1", "answers": answers})[1][
        "recorded"
    ] == len(answers)
    assert eve.call("POST", "/api/answer", {"task": "TC-1", "answers": answers})[0] == 400
    status, thread = eve.call(
        "POST", "/api/chat", {"task": "TC-1", "text": "What splice loss is allowed?"}
    )
    assert status == 200
    posts = eve.call("GET", "/api/me")[1]["threads"][0]["posts"]
    assert [p["assistant"] for p in posts] == [False, True]  # the assistant answered at once
    assert "under 0.5 dB" in posts[1]["text"]
    assert eve.call("POST", "/api/done", {"thread": thread["thread"]})[0] == 200
    assert eve.call("GET", "/api/me")[1]["threads"][0]["closed"] is True
    assert eve.call("POST", "/api/ticket", {"text": "page broken"})[0] == 200
    assert eve.call("POST", "/api/unknown", {})[0] == 404


def test_giving_taking_handing_in_and_sending_back_a_task(app):
    play(app, 1)
    max_, eve = app.client("max"), app.client("eve")
    new = {
        "title": "Label the closures",
        "assignee": "eve",
        "spec": SPEC,
        "project": "SURF Illinois",
        "due": "2026-10-30",
    }
    assert eve.call("POST", "/api/task/new", new)[0] == 400  # not an issuer
    status, made = max_.call("POST", "/api/task/new", new)
    assert status == 200 and made["task"] == "APP-0001"
    mine = eve.call("GET", "/api/me")[1]["mine"]
    task = next(c for c in mine if c["task_id"] == "APP-0001")
    assert task["can_accept"] and task["brief"]["goal"].startswith("Install four handholes")
    assert eve.call("POST", "/api/task/accept", {"task": "APP-0001"})[0] == 200
    assert max_.call("POST", "/api/task/accept", {"task": "APP-0001"})[0] == 400
    assert eve.call("POST", "/api/task/submit", {"task": "APP-0001", "note": "done"})[0] == 200
    assert eve.call("POST", "/api/task/return", {"task": "APP-0001", "note": "no"})[0] == 400
    assert (
        max_.call("POST", "/api/task/return", {"task": "APP-0001", "note": "photos are missing"})[0]
        == 200
    )
    after = next(c for c in eve.call("GET", "/api/me")[1]["mine"] if c["task_id"] == "APP-0001")
    assert after["task_status"] == "Working on it"


def test_the_spec_assistant_structures_the_text_and_asks_what_is_missing(app):
    max_ = app.client("max")
    status, brief = max_.call(
        "POST", "/api/assist/spec", {"spec": "Fix the cable soon.", "assignee": "eve", "due": ""}
    )
    assert status == 200 and brief["goal"] == "Fix the cable soon."
    assert any("'soon'" in g for g in brief["gaps"]) and any(
        "No due date" in g for g in brief["gaps"]
    )
    assert any("Name the commands" in g for g in brief["gaps"])  # Eve is an engineer


def test_a_busy_run_is_a_503_not_a_race(app):
    play(app, 2)
    eve = app.client("eve")
    with hold_run_lock(app.state.base.out_dir):
        status, error = eve.call("POST", "/api/chat", {"task": "TC-1", "text": "hi"})
    assert status == 503 and "try again" in error["error"]


def place_call(app, purpose="verification", answered="pending"):
    """What a run leaves when it has placed a call: a row waiting for the provider's word."""
    with closing(sqlite3.connect(app.state.base.out_dir / "state" / "engineering.db")) as db:
        db.execute(
            "INSERT INTO call_results VALUES ('call-1', 'eve', 'TC-1', ?, ?, '2026-10-22')",
            (answered, purpose),
        )
        db.commit()


def test_the_webhook_needs_the_right_secret_and_secrets_stay_out_of_repr(app):
    play(app, 2)
    place_call(app)
    anyone = Client(app.server)
    assert (
        anyone.call(
            "POST", "/api/webhooks/call", {"call_key": "call-1"}, headers={"X-Webhook-Secret": ""}
        )[0]
        == 403
    )
    assert (
        anyone.call(
            "POST",
            "/api/webhooks/call",
            {"call_key": "call-1"},
            headers={"X-Webhook-Secret": "wrong"},
        )[0]
        == 403
    )
    assert SECRET not in repr(app.state.base.team)


def test_the_webhook_settles_a_pending_call_and_takes_the_answers_once(app):
    play(app, 2)
    place_call(app)
    secret = {"X-Webhook-Secret": SECRET}
    provider = Client(app.server)
    board = app.client("eve").call("GET", "/api/me")[1]
    answers = {
        str(q["number"]): "under 0.5 dB" for q in board["mine"][0]["questions"] if q["graded"]
    }
    body = {"call_key": "call-1", "answered_by": "person", "answers": answers}
    status, result = provider.call(
        "POST", "/api/webhooks/call", body, headers={**secret, "Host": "public.example"}
    )
    assert status == 200 and result["recorded"] == len(answers)
    again, error = provider.call(
        "POST", "/api/webhooks/call", {"call_key": "call-1", "answers": answers}, headers=secret
    )
    assert again == 400 and "already answered" in error["error"]
    with closing(sqlite3.connect(app.state.base.out_dir / "state" / "engineering.db")) as db:
        assert db.execute("SELECT answered_by FROM call_results").fetchone() == ("person",)


def test_the_webhook_refuses_unknown_calls_reminders_and_unanswered_calls(app):
    play(app, 2)
    secret = {"X-Webhook-Secret": SECRET}
    provider = Client(app.server)
    assert (
        provider.call("POST", "/api/webhooks/call", {"call_key": "nope"}, headers=secret)[0] == 400
    )
    place_call(app, purpose="reminder")
    status, error = provider.call(
        "POST", "/api/webhooks/call", {"call_key": "call-1", "answers": {"1": "x"}}, headers=secret
    )
    assert status == 400 and "reminder" in error["error"]
    assert (
        provider.call(
            "POST",
            "/api/webhooks/call",
            {"call_key": "call-1", "answered_by": "bogus"},
            headers=secret,
        )[0]
        == 400
    )
    assert (
        provider.call(
            "POST",
            "/api/webhooks/call",
            {"call_key": "call-1", "answered_by": "none"},
            headers=secret,
        )[0]
        == 200
    )
    assert (
        provider.call(
            "POST",
            "/api/webhooks/call",
            {"call_key": "call-1", "answers": {"1": "x"}},
            headers=secret,
        )[0]
        == 400
    )
