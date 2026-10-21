from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx2
import pytest

from followup.api import ApiError, MessagesApi, TasksApi, request_with_retries
from followup.mock_api import MockBackend, parse_faults
from followup.retry import RetryPolicy, parse_retry_after


class Sleeps(list):
    def __call__(self, seconds: float) -> None:
        self.append(seconds)


def policy(sleeps: Sleeps, **overrides) -> RetryPolicy:
    return RetryPolicy(sleep=sleeps, jitter=lambda: 0.0, **overrides)


def test_backoff_grows_exponentially_and_is_capped():
    p = RetryPolicy(max_attempts=10, base_delay=0.5, max_delay=4.0, jitter=lambda: 1.0)

    assert [p.delay(n) for n in range(1, 10)] == [0.5, 1.0, 2.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0]
    assert p.delay(10) is None


def test_jitter_stays_between_half_and_full_backoff():
    low = RetryPolicy(jitter=lambda: 0.0).delay(3)
    high = RetryPolicy(jitter=lambda: 1.0).delay(3)

    assert (low, high) == (1.0, 2.0)


def test_retry_after_is_obeyed_or_ends_the_retries_when_too_long():
    p = RetryPolicy(max_delay=8.0)

    assert p.delay(1, retry_after=3.0) == 3.0
    assert p.delay(1, retry_after=60.0) is None


def test_parse_retry_after_supports_seconds_and_http_dates():
    now = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)

    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("Wed, 21 Oct 2026 12:00:30 GMT", now=now) == 30.0
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None


def test_tasks_are_paged_until_the_last_cursor(fixture_records):
    backend = MockBackend(fixture_records)

    records = TasksApi(backend.client(), RetryPolicy(), page_size=3).list_tasks()

    assert records == fixture_records
    assert backend.calls == ["GET /tasks -> 200"] * 4


def test_rate_limit_and_transient_errors_are_retried_with_backoff(fixture_records):
    sleeps = Sleeps()
    backend = MockBackend(fixture_records, faults={"tasks": ["429", "503", "timeout"]})

    records = TasksApi(backend.client(), policy(sleeps)).list_tasks()

    assert len(records) == 10
    assert backend.calls == [
        "GET /tasks -> 429",
        "GET /tasks -> 503",
        "GET /tasks -> timeout",
        "GET /tasks -> 200",
    ]
    assert sleeps == [1.0, 0.5, 1.0]  # Retry-After: 1, then backoff for attempts 2 and 3


def test_persistent_outage_gives_up_after_bounded_attempts(fixture_records):
    sleeps = Sleeps()
    backend = MockBackend(fixture_records, faults={"tasks": ["503"] * 10})

    with pytest.raises(ApiError, match=r"HTTP 503, gave up after 4 attempt\(s\)"):
        TasksApi(backend.client(), policy(sleeps)).list_tasks()

    assert len(backend.calls) == 4
    assert len(sleeps) == 3


def test_non_retryable_status_is_returned_at_once():
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.path)
        return httpx2.Response(404)

    http = httpx2.Client(base_url="https://x", transport=httpx2.MockTransport(handler))

    assert request_with_retries(http, RetryPolicy(), "GET", "/missing").status_code == 404
    assert calls == ["/missing"]


def test_malformed_page_is_an_api_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"tasks": []})

    http = httpx2.Client(base_url="https://x", transport=httpx2.MockTransport(handler))

    with pytest.raises(ApiError, match="no 'items' list"):
        TasksApi(http, RetryPolicy()).list_tasks()


def test_provider_deduplicates_by_idempotency_key_and_persists_the_log(tmp_path):
    log = tmp_path / "delivery_log.jsonl"
    backend = MockBackend([], delivery_log=log)
    api = MessagesApi(backend.client(), RetryPolicy())
    payload = {"to": "warehouse", "task_id": "TC-1002", "decision": "request_eta", "body": "ETA?"}

    first = api.post("key-1", payload)
    second = api.post("key-1", payload)

    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["message_id"] == second.json()["message_id"] == "msg-0001"
    assert len(log.read_text().splitlines()) == 1

    restarted = MessagesApi(MockBackend([], delivery_log=log).client(), RetryPolicy())
    assert restarted.find("key-1")["message_id"] == "msg-0001"
    assert restarted.find("key-2") is None


def test_provider_rejects_writes_without_idempotency_key():
    backend = MockBackend([])

    response = backend.client().post("/messages", json={"to": "a", "task_id": "b", "body": "c"})

    assert response.status_code == 400
    assert backend.messages == {}


def test_timeout_after_commit_stores_the_message_but_loses_the_response():
    backend = MockBackend([], faults={"send": ["timeout-after-commit"]})
    api = MessagesApi(backend.client(), RetryPolicy())

    with pytest.raises(httpx2.ReadTimeout):
        api.post("key-1", {"to": "a", "task_id": "TC-1", "body": "hello"})

    assert list(backend.messages) == ["key-1"]
    assert backend.calls == ["POST /messages -> 201, lost"]


def test_fault_specs_are_validated():
    assert parse_faults(["tasks=429, 503", "send=timeout-after-commit"]) == {
        "tasks": ["429", "503"],
        "send": ["timeout-after-commit"],
    }
    with pytest.raises(ValueError, match="unknown fault route"):
        parse_faults(["inbox=429"])
    with pytest.raises(ValueError, match="unknown fault"):
        parse_faults(["tasks=418"])


def test_delivery_log_lines_are_json(tmp_path):
    log = tmp_path / "log.jsonl"
    api = MessagesApi(MockBackend([], delivery_log=log).client(), RetryPolicy())

    api.post("k", {"to": "John Smith", "task_id": "TC-1001", "decision": "x", "body": "Hi"})

    record = json.loads(log.read_text())
    assert record["to"] == "John Smith"
    assert record["idempotency_key"] == "k"
