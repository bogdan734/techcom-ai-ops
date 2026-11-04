"""In-process stand-ins for the tasks API and the messaging provider.

They are served through ``httpx2.MockTransport``, so the service's HTTP code (status codes,
headers, timeouts, retries) runs exactly as it would against a remote API. Faults (see
:mod:`followup.faults`) are scripted per route to reproduce rate limiting, outages and a
write that is committed while its response is lost.

The messaging provider honours ``Idempotency-Key`` and persists what it "sent" to an
append-only JSONL file: that file is the mock delivery log.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2

MAX_PAGE = 500


class MockBackend:
    def __init__(
        self,
        tasks: list[Any],
        delivery_log: Path | None = None,
        faults: Mapping[str, Iterable[str]] | None = None,
        call_log: Path | None = None,
    ):
        self.tasks = list(tasks)
        self.delivery_log = delivery_log
        self.call_log = call_log
        self.call_records: dict[str, dict[str, Any]] = {}
        self._answered_by = "person"
        self.faults = {route: deque(items) for route, items in (faults or {}).items()}
        self.messages: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        if delivery_log is not None and delivery_log.exists():
            for line in delivery_log.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                self.messages[record["idempotency_key"]] = record
        if call_log is not None and call_log.exists():
            for line in call_log.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                self.call_records[record["idempotency_key"]] = record

    def client(self) -> httpx2.Client:
        return httpx2.Client(base_url="https://mock.local", transport=httpx2.MockTransport(self))

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        route = self._route(request)
        fault = self._next_fault(route)
        if fault in _CALL_OUTCOMES:
            self._answered_by, fault = _CALL_OUTCOMES[fault], None
        if fault == "timeout":
            self.calls.append(f"{request.method} {request.url.path} -> timeout")
            raise httpx2.ReadTimeout("mock: no response", request=request)
        if fault is not None and fault.isdigit():
            self.calls.append(f"{request.method} {request.url.path} -> {fault}")
            headers = {"Retry-After": "1"} if fault == "429" else {}
            return httpx2.Response(int(fault), headers=headers, json={"error": "injected"})

        response = self._dispatch(request)
        if fault == "timeout-after-commit":
            self.calls.append(
                f"{request.method} {request.url.path} -> {response.status_code}, lost"
            )
            raise httpx2.ReadTimeout("mock: committed, response lost", request=request)
        self.calls.append(f"{request.method} {request.url.path} -> {response.status_code}")
        return response

    def _route(self, request: httpx2.Request) -> str | None:
        path = request.url.path
        if request.method == "GET" and path == "/tasks":
            return "tasks"
        if request.method == "POST" and path == "/messages":
            return "send"
        if request.method == "GET" and path.startswith("/messages/"):
            return "lookup"
        if request.method == "POST" and path == "/calls":
            return "call"
        if request.method == "GET" and path.startswith("/calls/"):
            return "call_lookup"
        return None

    def _next_fault(self, route: str | None) -> str | None:
        queue = self.faults.get(route or "")
        return queue.popleft() if queue else None

    def _dispatch(self, request: httpx2.Request) -> httpx2.Response:
        route = self._route(request)
        if route == "tasks":
            return self._list_tasks(request)
        if route == "send":
            return self._send(request)
        if route == "lookup":
            key = request.url.path.removeprefix("/messages/")
            record = self.messages.get(key)
            return httpx2.Response(200, json=record) if record else _error(404, "not found")
        if route == "call":
            return self._place_call(request)
        if route == "call_lookup":
            record = self.call_records.get(request.url.path.removeprefix("/calls/"))
            return httpx2.Response(200, json=record) if record else _error(404, "not found")
        return _error(404, "no such route")

    def _list_tasks(self, request: httpx2.Request) -> httpx2.Response:
        try:
            limit = min(MAX_PAGE, max(1, int(request.url.params.get("limit", "100"))))
            offset = int(request.url.params.get("cursor", "0"))
        except ValueError:
            return _error(400, "limit and cursor must be integers")
        page = self.tasks[offset : offset + limit]
        more = offset + limit < len(self.tasks)
        return httpx2.Response(
            200, json={"items": page, "next_cursor": str(offset + limit) if more else None}
        )

    def _send(self, request: httpx2.Request) -> httpx2.Response:
        key = request.headers.get("Idempotency-Key")
        if not key:
            return _error(400, "Idempotency-Key header is required")
        existing = self.messages.get(key)
        if existing:
            return httpx2.Response(200, json=existing)
        try:
            body = json.loads(request.content)
        except ValueError:
            return _error(400, "body must be JSON")
        if not isinstance(body, dict) or not all(body.get(f) for f in ("to", "task_id", "body")):
            return _error(422, "to, task_id and body are required")
        record = {
            "message_id": f"msg-{len(self.messages) + 1:04d}",
            "idempotency_key": key,
            "sent_at": datetime.now(UTC).isoformat(timespec="seconds"),
            **{f: body.get(f) for f in ("task_id", "decision", "to", "body")},
        }
        self.messages[key] = record
        if self.delivery_log is not None:
            self.delivery_log.parent.mkdir(parents=True, exist_ok=True)
            with self.delivery_log.open("a", encoding="utf-8") as log:
                log.write(json.dumps(record, ensure_ascii=False) + "\n")
        return httpx2.Response(201, json=record)

    def _place_call(self, request: httpx2.Request) -> httpx2.Response:
        """Like ``_send``, for the voice provider: one call per key, and who picked up."""
        key = request.headers.get("Idempotency-Key")
        if not key:
            return _error(400, "Idempotency-Key header is required")
        existing = self.call_records.get(key)
        if existing:
            return httpx2.Response(200, json=existing)
        try:
            body = json.loads(request.content)
        except ValueError:
            return _error(400, "body must be JSON")
        if not isinstance(body, dict) or not all(body.get(f) for f in ("to", "task_id", "body")):
            return _error(422, "to, task_id and body are required")
        record = {
            "message_id": f"call-{len(self.call_records) + 1:04d}",
            "idempotency_key": key,
            "placed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "answered_by": self._answered_by,
            **{f: body.get(f) for f in ("task_id", "decision", "to", "body")},
        }
        self._answered_by = "person"
        self.call_records[key] = record
        if self.call_log is not None:
            self.call_log.parent.mkdir(parents=True, exist_ok=True)
            with self.call_log.open("a", encoding="utf-8") as log:
                log.write(json.dumps(record, ensure_ascii=False) + "\n")
        return httpx2.Response(201, json=record)


_CALL_OUTCOMES = {"machine": "machine", "no-answer": "none"}


def _error(status: int, detail: str) -> httpx2.Response:
    return httpx2.Response(status, json={"error": detail})
