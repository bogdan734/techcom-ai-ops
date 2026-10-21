"""HTTP clients for the tasks API and the messaging provider."""

from __future__ import annotations

import logging
from typing import Any

import httpx2

from .audit import emit
from .retry import RetryPolicy, parse_retry_after

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class ApiError(Exception):
    """The API refused the request, or stayed unavailable after bounded retries."""


def request_with_retries(
    http: httpx2.Client, policy: RetryPolicy, method: str, url: str, **kwargs: Any
) -> httpx2.Response:
    """Send an idempotent request, retrying rate limits, 5xx and transport errors.

    Non-retryable responses (2xx, 4xx other than 429) are returned for the caller to
    interpret. Never use this for a write without an idempotency key.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            response = http.request(method, url, **kwargs)
        except httpx2.TransportError as exc:
            failure, retry_after = type(exc).__name__, None
        else:
            if response.status_code not in RETRYABLE_STATUS:
                return response
            failure = f"HTTP {response.status_code}"
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
        delay = policy.delay(attempt, retry_after)
        if delay is None:
            raise ApiError(f"{method} {url}: {failure}, gave up after {attempt} attempt(s)")
        emit(
            "http_retry",
            f"{method} {url}: {failure}, retry {attempt}/{policy.max_attempts - 1} in {delay:.2f}s",
            level=logging.WARNING,
            method=method,
            url=url,
            failure=failure,
            attempt=attempt,
            delay=round(delay, 3),
        )
        policy.sleep(delay)


class TasksApi:
    def __init__(self, http: httpx2.Client, policy: RetryPolicy, page_size: int = 100):
        self.http = http
        self.policy = policy
        self.page_size = page_size

    def list_tasks(self) -> list[Any]:
        """All task records, following ``next_cursor`` until the last page."""
        records: list[Any] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": self.page_size}
            if cursor:
                params["cursor"] = cursor
            response = request_with_retries(self.http, self.policy, "GET", "/tasks", params=params)
            page = _json_object(response, "GET /tasks")
            items = page.get("items")
            if not isinstance(items, list):
                raise ApiError("GET /tasks: response has no 'items' list")
            records.extend(items)
            cursor = page.get("next_cursor")
            if not cursor:
                return records


class MessagesApi:
    def __init__(self, http: httpx2.Client, policy: RetryPolicy):
        self.http = http
        self.policy = policy

    def post(self, idempotency_key: str, payload: dict[str, Any]) -> httpx2.Response:
        """Exactly one attempt: a lost response is ambiguous, so the outbox owns retries."""
        return self.http.post(
            "/messages", json=payload, headers={"Idempotency-Key": idempotency_key}
        )

    def find(self, idempotency_key: str) -> dict[str, Any] | None:
        """The message sent under this key, or None if the provider never committed it."""
        url = f"/messages/{idempotency_key}"
        response = request_with_retries(self.http, self.policy, "GET", url)
        if response.status_code == 404:
            return None
        return _json_object(response, f"GET {url}")


def _json_object(response: httpx2.Response, what: str) -> dict[str, Any]:
    if response.status_code != 200:
        raise ApiError(f"{what}: unexpected HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise ApiError(f"{what}: response is not JSON") from exc
    if not isinstance(data, dict):
        raise ApiError(f"{what}: expected a JSON object")
    return data
