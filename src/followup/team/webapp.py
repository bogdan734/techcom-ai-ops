"""The app: a local web interface over the same functions as the console.

It binds to 127.0.0.1, answers only requests addressed to a local host name, and takes changes
only as JSON with a custom header, so a page on another origin cannot drive it. The person at
the top is chosen, not authenticated; the README says what a real deployment puts in front.
With a scenario file the app steps through a few days at the push of a button, which is how a
reviewer sees the layer work without a scheduler.
"""

from __future__ import annotations

import argparse
import hmac
import json
import shutil
import threading
import webbrowser
from dataclasses import replace
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from followup.config import ConfigError, Settings
from followup.faults import parse_faults
from followup.pipeline import run
from followup.run_lock import RunBusy, hold_run_lock

from . import actions
from .calls import AnsweredBy, settle_result
from .context import ActionError, Context, load_context, read_all
from .dashboard import build_dashboard
from .org import Role
from .vault import VaultError

UI = Path(__file__).with_name("ui") / "index.html"
MAX_BODY = 64 * 1024
PROGRESS = "app-progress.json"
WEBHOOK = "/api/webhooks/call"
CSP = "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:"


class Day:
    def __init__(self, raw: dict[str, Any], base: Path) -> None:
        self.date = date.fromisoformat(raw["date"])
        self.tasks = (base / raw["tasks"]).resolve()
        self.label = raw.get("label", raw["date"])
        self.title = raw.get("title", "")
        self.note = raw.get("note", "")
        self.faults = list(raw.get("faults", []))


class AppState:
    """Where the demo is: which days have run, and the settings for the day on screen."""

    def __init__(self, settings: Settings, scenario: Path | None) -> None:
        self.base = settings
        self.title = ""
        self.days: list[Day] = []
        if scenario is not None:
            raw = json.loads(scenario.read_text(encoding="utf-8"))
            self.title = raw.get("title", "")
            self.days = [Day(d, scenario.parent) for d in raw["days"]]
        self.guard = threading.Lock()

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
            "trust_visibility": self.base.team.trust_visibility if self.base.team else "hidden",
            "days": [
                {
                    "label": d.label,
                    "title": d.title,
                    "note": d.note,
                    "date": d.date.isoformat(),
                    "done": i < done,
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


def serve_app(settings: Settings, args: argparse.Namespace) -> int:
    if settings.team is None:
        raise ConfigError("no org file: set ORG_FILE or pass --org")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError("the app has no sign-in: it only listens on this computer")
    if args.fresh:
        shutil.rmtree(settings.out_dir, ignore_errors=True)
    scenario = Path(args.scenario) if args.scenario else None
    server = make_server(AppState(settings, scenario), args.host, args.port)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"followup app on {url}  (Ctrl-C to stop)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


class _Handler(BaseHTTPRequestHandler):
    app: AppState
    hosts: set[str]
    server_version = "followup"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - quiet by default
        return

    # -- routing ------------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._handle(post=False)

    def do_POST(self) -> None:  # noqa: N802
        self._handle(post=True)

    def _handle(self, post: bool) -> None:
        try:
            if post and urlparse(self.path).path == WEBHOOK:
                # A provider calls this from outside: the shared secret stands in for the
                # local-only checks below.
                return self._json(200, self._webhook(self._body()))
            if self.headers.get("Host", "") not in self.hosts:
                return self._json(403, {"error": "this app answers only on its own address"})
            url = urlparse(self.path)
            if not post and url.path == "/":
                return self._file()
            if post and (
                self.headers.get("X-Followup") != "1"
                or "application/json" not in self.headers.get("Content-Type", "")
            ):
                return self._json(403, {"error": "changes need the app's own request header"})
            body = self._body() if post else {}
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            return self._json(200, self._route(url.path, query, body, post))
        except ActionError as exc:
            self._json(400, {"error": str(exc)})
        except PermissionError as exc:
            self._json(403, {"error": str(exc)})
        except RunBusy:
            self._json(503, {"error": "a run is working right now; try again in a moment"})
        except (ConfigError, VaultError, KeyError, ValueError) as exc:
            self._json(400, {"error": str(exc)})
        except _NotFound:
            self._json(404, {"error": "no such page"})

    def _route(self, path: str, query: dict, body: dict, post: bool) -> Any:
        if not post:
            if path == "/api/state":
                return self.app.describe()
            if path == "/api/people":
                return self._people()
            if path == "/api/me":
                ctx = self._context()
                return build_dashboard(ctx, ctx.person(query.get("as", ""))).to_dict()
            raise _NotFound
        if path == "/api/advance":
            return self.app.advance()
        ctx = self._context()
        viewer = ctx.person(str(body.get("as", "")))
        with hold_run_lock(ctx.settings.out_dir, ctx.settings.run_lock):
            return self._act(path, ctx, viewer, body)

    def _act(self, path: str, ctx: Context, viewer: Any, body: dict) -> dict[str, Any]:
        if path == "/api/answer":
            answers = {int(n): str(t) for n, t in dict(body.get("answers", {})).items()}
            return {
                "recorded": actions.submit_answers(ctx, viewer, str(body.get("task", "")), answers)
            }
        if path == "/api/review":
            actions.review(
                ctx,
                viewer,
                str(body.get("task", "")),
                bool(body.get("accept")),
                str(body.get("note", "")),
            )
            return {"ok": True}
        if path == "/api/clear":
            actions.clear(ctx, viewer, str(body.get("person", "")))
            return {"ok": True}
        if path == "/api/chat":
            return {
                "thread": actions.say(
                    ctx, viewer, str(body.get("task", "")), str(body.get("text", ""))
                )
            }
        if path == "/api/reply":
            actions.reply(ctx, viewer, str(body.get("thread", "")), str(body.get("text", "")))
            return {"ok": True}
        if path == "/api/done":
            actions.finish(ctx, viewer, str(body.get("thread", "")))
            return {"ok": True}
        if path == "/api/ticket":
            return {"thread": actions.open_ticket(ctx, viewer, str(body.get("text", "")))}
        raise _NotFound

    # -- helpers ------------------------------------------------------------------------

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

    def _context(self) -> Context:
        return load_context(self.app.settings())

    def _people(self) -> list[dict[str, str]]:
        org = self._context().org
        return [
            {"id": p.id, "name": p.name, "role": p.role.value, "department": p.department}
            for p in org.people
            if p.role not in (Role.PROJECT_MANAGER, Role.WAREHOUSE)
        ]

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
        self.end_headers()
        self.wfile.write(raw)

    def _file(self) -> None:
        raw = UI.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(raw)


class _NotFound(Exception):
    pass
