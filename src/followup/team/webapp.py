"""The app: a local web interface where each person signs in to their own account.

It binds to 127.0.0.1, answers only requests addressed to a local host name, and takes changes
only as JSON with a custom header from a page of its own origin, so a page elsewhere cannot drive
it. Who is acting always comes from the session cookie and never from the request, so a person
cannot become someone else by changing a name. A voice provider's webhook is the one door with
another kind of key: a shared secret. With a scenario file the app steps through a few days at the
push of a button, which is how a reviewer sees the layer work without a scheduler.
"""

from __future__ import annotations

import argparse
import hmac
import json
import shutil
import threading
import webbrowser
from dataclasses import replace
from datetime import UTC, date, datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from followup.config import ConfigError, Settings
from followup.faults import parse_faults
from followup.llm import LLMGateway
from followup.pipeline import make_llm, run
from followup.run_lock import RunBusy, hold_run_lock

from . import accounts, actions
from .brief import structure
from .calls import AnsweredBy, settle_result
from .context import ActionError, Context, load_context, read_all
from .dashboard import build_dashboard
from .org import Capability, Person, Role
from .schema import CONTROL
from .vault import Vault, VaultError

UI = Path(__file__).with_name("ui") / "index.html"
MAX_BODY = 64 * 1024
PROGRESS = "app-progress.json"
WEBHOOK = "/api/webhooks/call"
COOKIE = "fd_session"
CSP = "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:"
DEMO_PASSWORD = "demo-{id}-pass"


class Day:
    def __init__(self, raw: dict[str, Any], base: Path) -> None:
        self.date = date.fromisoformat(raw["date"])
        self.tasks = (base / raw["tasks"]).resolve()
        self.label = raw.get("label", raw["date"])
        self.title = raw.get("title", "")
        self.note = raw.get("note", "")
        self.translations = {k: v for k, v in raw.items() if k.startswith(("title_", "note_"))}
        self.faults = list(raw.get("faults", []))


class AppState:
    """Where the demo is: which days have run, and the settings for the day on screen."""

    def __init__(
        self, settings: Settings, scenario: Path | None, demo_logins: dict[str, str] | None = None
    ) -> None:
        self.base = settings
        self.title = ""
        self.days: list[Day] = []
        self.demo_logins = demo_logins or {}
        self.guard = threading.Lock()
        self._gateway: LLMGateway | None = None
        if scenario is not None:
            raw = json.loads(scenario.read_text(encoding="utf-8"))
            self.title = raw.get("title", "")
            self.days = [Day(d, scenario.parent) for d in raw["days"]]

    @property
    def gateway(self) -> LLMGateway:
        """One model gateway for the assistant's instant answers, made when first needed."""
        if self._gateway is None:
            self._gateway = LLMGateway(make_llm(self.base), self.base.llm_max_calls)
        return self._gateway

    @property
    def done(self) -> int:
        path = self.base.out_dir / PROGRESS
        if not self.days or not path.exists():
            return 0
        return min(json.loads(path.read_text(encoding="utf-8")).get("done", 0), len(self.days))

    def settings(self) -> Settings:
        """The day on screen: the last one that ran, or the first before anything ran."""
        if not self.days:
            return self.base
        day = self.days[max(self.done - 1, 0)]
        return replace(self.base, tasks_file=day.tasks, today=day.date)

    def describe(self) -> dict[str, Any]:
        done = self.done
        return {
            "mode": "scenario" if self.days else "live",
            "title": self.title,
            "done": done,
            "today": self.settings().today.isoformat(),
            "days": [
                {
                    "label": d.label,
                    "title": d.title,
                    "note": d.note,
                    "date": d.date.isoformat(),
                    "done": i < done,
                    **d.translations,
                }
                for i, d in enumerate(self.days)
            ],
        }

    def advance(self) -> dict[str, Any]:
        with self.guard:
            if self.days:
                done = self.done
                if done >= len(self.days):
                    raise ActionError("the scenario has no more days")
                day = self.days[done]
                settings = replace(self.base, tasks_file=day.tasks, today=day.date)
                faults = parse_faults(day.faults)
            else:
                settings, faults, done = self.base, {}, 0
            report = run(settings, faults)
            if self.days:
                (self.base.out_dir / PROGRESS).write_text(json.dumps({"done": done + 1}), "utf-8")
            team = report.team
            return {
                "date": settings.today.isoformat(),
                "exit_code": report.exit_code,
                "notices": team.messages if team else 0,
                "undelivered": team.undelivered if team else 0,
                "replay": team.replay if team else "new",
            }


def make_server(state: AppState, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    class Handler(_Handler):
        app = state

    server = ThreadingHTTPServer((host, port), Handler)
    Handler.hosts = {f"127.0.0.1:{server.server_port}", f"localhost:{server.server_port}"}
    return server


def prepare(settings: Settings, demo: bool) -> dict[str, str]:
    """Create every database file, and in demo mode an account per person with a known password."""
    team = settings.team
    assert team is not None
    vault = Vault(settings.out_dir / "state", team.org, team.trust_visibility)
    for name in vault.known():
        vault.ensure(name)
    shown: dict[str, str] = {}
    if demo:
        db = vault.open(CONTROL)
        try:
            for person in team.org.people:
                if person.role in (Role.PROJECT_MANAGER, Role.WAREHOUSE):
                    continue
                password = DEMO_PASSWORD.format(id=person.id)
                if person.id not in accounts.accounts(db):
                    accounts.set_password(db, person.id, password, settings.today)
                shown[person.id] = password
        finally:
            db.close()
    return shown


def serve_app(settings: Settings, args: argparse.Namespace) -> int:
    if settings.team is None:
        raise ConfigError("no org file: set ORG_FILE or pass --org")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError("the app only listens on this computer; put a reverse proxy in front")
    if args.fresh:
        shutil.rmtree(settings.out_dir, ignore_errors=True)
    shown = prepare(settings, args.demo_logins)
    scenario = Path(args.scenario) if args.scenario else None
    server = make_server(AppState(settings, scenario, shown), args.host, args.port)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"followup app on {url}  (Ctrl-C to stop)")
    if shown:
        print(
            "DEMO LOGINS ARE ON: each account's password is demo-<id>-pass, "
            "shown on the sign-in page"
        )
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


class _NotFound(Exception):
    pass


class _Unauthorized(Exception):
    pass


class _Handler(BaseHTTPRequestHandler):
    app: AppState
    hosts: set[str]
    server_version = "followup"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - quiet by default
        return

    def do_GET(self) -> None:  # noqa: N802
        self._handle(post=False)

    def do_POST(self) -> None:  # noqa: N802
        self._handle(post=True)

    def _handle(self, post: bool) -> None:
        self._cookie_out: str | None = None
        try:
            path = urlparse(self.path).path
            if post and path == WEBHOOK:
                # A provider calls this from outside: the shared secret stands in for the
                # local-only checks below.
                return self._json(200, self._webhook(self._body()))
            if self.headers.get("Host", "") not in self.hosts:
                return self._json(403, {"error": "this app answers only on its own address"})
            if not post and path == "/":
                return self._file()
            if post and not self._same_origin():
                return self._json(403, {"error": "changes need the app's own request header"})
            body = self._body() if post else {}
            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            return self._json(200, self._route(path, query, body, post))
        except _Unauthorized:
            self._json(401, {"error": "sign in first"})
        except (
            ActionError,
            ConfigError,
            VaultError,
            KeyError,
            ValueError,
            accounts.AuthError,
        ) as exc:
            self._json(400, {"error": str(exc)})
        except PermissionError as exc:
            self._json(403, {"error": str(exc)})
        except RunBusy:
            self._json(503, {"error": "a run is working right now; try again in a moment"})
        except _NotFound:
            self._json(404, {"error": "no such page"})

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        local = origin is None or origin in {f"http://{h}" for h in self.hosts}
        return (
            local
            and self.headers.get("X-Followup") == "1"
            and "application/json" in self.headers.get("Content-Type", "")
        )

    # -- routing ------------------------------------------------------------------------

    def _route(self, path: str, query: dict, body: dict, post: bool) -> Any:
        if post and path == "/api/login":
            return self._login(body)
        if not post and path == "/api/demo":
            return [{"id": i, "password": p} for i, p in self.app.demo_logins.items()]
        if not post and path == "/api/session":
            try:
                viewer = self._viewer()
            except _Unauthorized:
                return {"person": None}  # not an error: the page asks this to decide what to show
            return {"person": _person(viewer), "scenario": self.app.describe()}
        viewer = self._viewer()
        if post and path == "/api/logout":
            self._with_control(lambda db: accounts.end_session(db, self._token()))
            self._cookie_out = f"{COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"
            return {"ok": True}
        if not post:
            return self._read(path, viewer)
        if path == "/api/advance":
            if not self._context().org.can(viewer.id, Capability.ISSUE_TASKS):
                raise PermissionError("only a manager can move the days along")
            return self.app.advance()
        if path == "/api/assist/spec":
            role = self._context().org.find(str(body.get("assignee", "")))
            return structure(
                str(body.get("spec", "")),
                role.role.value if role else None,
                str(body.get("due", "")),
            ).to_dict()
        ctx = self._context()
        with hold_run_lock(ctx.settings.out_dir, ctx.settings.run_lock):
            return self._act(path, ctx, viewer, body)

    def _read(self, path: str, viewer: Person) -> Any:
        if path == "/api/me":
            return build_dashboard(self._context(), viewer).to_dict()
        raise _NotFound

    def _act(self, path: str, ctx: Context, viewer: Person, body: dict) -> dict[str, Any]:
        text = lambda name: str(body.get(name, ""))  # noqa: E731
        if path == "/api/answer":
            answers = {int(n): str(t) for n, t in dict(body.get("answers", {})).items()}
            return {"recorded": actions.submit_answers(ctx, viewer, text("task"), answers)}
        if path == "/api/review":
            actions.review(ctx, viewer, text("task"), bool(body.get("accept")), text("note"))
        elif path == "/api/clear":
            actions.clear(ctx, viewer, text("person"))
        elif path == "/api/chat":
            thread = actions.say(ctx, viewer, text("task"), text("text"))
            actions.assist_now(ctx, thread, self.app.gateway)
            return {"thread": thread}
        elif path == "/api/reply":
            actions.reply(ctx, viewer, text("thread"), text("text"))
        elif path == "/api/done":
            actions.finish(ctx, viewer, text("thread"))
        elif path == "/api/ticket":
            return {"thread": actions.open_ticket(ctx, viewer, text("text"))}
        elif path == "/api/task/new":
            task = actions.create_task(
                ctx,
                viewer,
                title=text("title"),
                assignee_id=text("assignee"),
                spec=text("spec"),
                project=text("project") or "General",
                due=text("due"),
            )
            return {"task": task}
        elif path == "/api/task/accept":
            actions.accept_task(ctx, viewer, text("task"))
        elif path == "/api/task/submit":
            actions.submit_task(ctx, viewer, text("task"), text("note"))
        elif path == "/api/task/return":
            actions.return_task(ctx, viewer, text("task"), text("note"))
        else:
            raise _NotFound
        return {"ok": True}

    # -- accounts -----------------------------------------------------------------------

    def _login(self, body: dict) -> dict[str, Any]:
        person, password = str(body.get("person", "")).strip(), str(body.get("password", ""))
        ctx = self._context()
        found = ctx.org.find(person)
        now = datetime.now(UTC)
        db = ctx.vault.open(CONTROL)
        try:
            if found is None or not accounts.verify(db, found.id, password, now):
                raise PermissionError("wrong id or password")
            token = accounts.start_session(db, found.id, now)
        finally:
            db.close()
        seconds = int(accounts.SESSION_FOR.total_seconds())
        self._cookie_out = f"{COOKIE}={token}; Max-Age={seconds}; Path=/; HttpOnly; SameSite=Strict"
        return {"person": _person(found)}

    def _token(self) -> str:
        jar: SimpleCookie = SimpleCookie(self.headers.get("Cookie", ""))
        return jar[COOKIE].value if COOKIE in jar else ""

    def _viewer(self) -> Person:
        ctx = self._context()
        db = ctx.vault.open(CONTROL)
        try:
            who = accounts.session_person(db, self._token(), datetime.now(UTC))
        finally:
            db.close()
        if who is None:
            raise _Unauthorized
        return ctx.org.person(who)

    def _with_control(self, work: Any) -> None:
        db = self._context().vault.open(CONTROL)
        try:
            work(db)
        finally:
            db.close()

    # -- the voice provider's door --------------------------------------------------------

    def _webhook(self, body: dict) -> dict[str, Any]:
        """A voice provider reports how a call ended, and what the person answered."""
        team = self.app.base.team
        secret = team.calls_webhook_secret if team else ""
        if not secret:
            raise _NotFound  # switched off unless a secret is configured
        if not hmac.compare_digest(self.headers.get("X-Webhook-Secret", ""), secret):
            raise PermissionError("the webhook secret does not match")
        ctx = self._context()
        key = str(body.get("call_key", ""))
        found = next(
            (
                (name, row)
                for name in ctx.vault.departments()
                for row in read_all(
                    ctx.open(name),
                    "SELECT person_id, task_id, purpose, answered_by FROM call_results "
                    "WHERE call_key = ?",
                    (key,),
                )
            ),
            None,
        )
        if found is None:
            raise ActionError("unknown call")
        department, (person_id, task_id, purpose, answered) = found
        with hold_run_lock(ctx.settings.out_dir, ctx.settings.run_lock):
            if body.get("answered_by"):
                try:
                    outcome = AnsweredBy(str(body["answered_by"]))
                except ValueError as exc:
                    raise ActionError("answered_by is person, machine, none or pending") from exc
                db = ctx.vault.open(department)
                try:
                    if settle_result(db, key, outcome):
                        answered = outcome.value
                finally:
                    db.close()
            recorded = 0
            if body.get("answers"):
                if purpose != "verification":
                    raise ActionError("this call was a reminder, not a verification call")
                if answered not in ("person", "pending"):
                    raise ActionError("nobody answered this call")
                answers = {int(n): str(t) for n, t in dict(body["answers"]).items()}
                recorded = actions.submit_answers(ctx, ctx.person(person_id), task_id, answers)
        return {"ok": True, "recorded": recorded}

    # -- helpers ------------------------------------------------------------------------

    def _context(self) -> Context:
        return load_context(self.app.settings())

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY:
            raise ActionError("that request is too large")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ActionError("expected a JSON object")
        return data

    def _json(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if self._cookie_out:
            self.send_header("Set-Cookie", self._cookie_out)
        self.end_headers()
        self.wfile.write(raw)

    def _file(self) -> None:
        raw = UI.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(raw)


def _person(person: Person) -> dict[str, str]:
    return {
        "id": person.id,
        "name": person.name,
        "role": person.role.value,
        "department": person.department,
    }
