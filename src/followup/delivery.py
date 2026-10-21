"""Outbox: every follow-up reaches the provider at most once, across retries, crashes and runs.

1. The action is claimed in SQLite as ``pending`` before anything is sent.
2. It is posted with its action id as ``Idempotency-Key``, so the provider deduplicates.
3. A timeout or a dropped connection leaves the outcome unknown: the provider is asked
   whether it holds a message under that key (reconciliation) before anything is resent.
4. An action left ``pending`` or ``failed`` by an earlier run is reconciled first, too.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx2

from .api import RETRYABLE_STATUS, ApiError, MessagesApi
from .audit import emit
from .models import Action
from .retry import CircuitBreaker, RetryPolicy, parse_retry_after
from .store import Store


@dataclass(frozen=True)
class Delivery:
    # sent | reconciled | duplicate_suppressed | failed | deferred
    status: str
    # The stored action this outcome is about: when another run claimed it first, its wording
    # is what went out, not the caller's.
    action: Action
    message_id: str | None = None
    detail: str = ""


class Outbox:
    def __init__(
        self,
        store: Store,
        messages: MessagesApi,
        policy: RetryPolicy,
        breaker: CircuitBreaker | None = None,
    ):
        self.store = store
        self.messages = messages
        self.policy = policy
        self.breaker = breaker or CircuitBreaker()

    def deliver(self, action: Action) -> Delivery:
        stored = self.store.claim(action)
        if stored.status == "sent":
            return Delivery(
                "duplicate_suppressed", stored.action, stored.message_id, "already delivered"
            )
        if self.breaker.open:
            emit(
                "delivery_deferred",
                f"{action.task_id}: provider unavailable this run, left pending for the next",
                level=logging.WARNING,
                task_id=action.task_id,
                action_id=action.action_id,
            )
            return Delivery("deferred", stored.action, detail="provider unavailable this run")
        try:
            if stored.attempts:
                # An earlier run may have reached the provider before it lost track.
                found = self.messages.find(action.action_id)
                if found is not None:
                    return self._delivered(stored.action, found.get("message_id"), "reconciled")
            delivery = self._send(stored.action)
        except ApiError as exc:
            self.store.mark_failed(action.action_id, str(exc))
            self.breaker.record_failure()
            emit(
                "delivery_failed",
                f"{action.task_id}: not delivered ({exc}); will retry next run",
                level=logging.ERROR,
                task_id=action.task_id,
                action_id=action.action_id,
                error=str(exc),
            )
            return Delivery("failed", stored.action, detail=str(exc))
        self.breaker.record_success()
        return delivery

    def _send(self, action: Action) -> Delivery:
        payload = {
            "to": action.target,
            "task_id": action.task_id,
            "decision": action.decision.value,
            "body": action.message,
        }
        attempt = 0
        while True:
            attempt += 1
            self.store.start_attempt(action.action_id)
            try:
                response = self.messages.post(action.action_id, payload)
            except httpx2.TransportError as exc:
                emit(
                    "delivery_ambiguous",
                    f"{action.task_id}: {type(exc).__name__} on send, outcome unknown; "
                    "checking with the provider before any retry",
                    level=logging.WARNING,
                    task_id=action.task_id,
                    action_id=action.action_id,
                    error=type(exc).__name__,
                )
                found = self.messages.find(action.action_id)
                if found is not None:
                    return self._delivered(action, found.get("message_id"), "reconciled")
                failure, retry_after = type(exc).__name__, None
            else:
                if response.is_success:
                    return self._delivered(action, _message_id(response), "sent")
                if response.status_code not in RETRYABLE_STATUS:
                    detail = response.text[:200]
                    raise ApiError(f"POST /messages: HTTP {response.status_code} {detail}")
                failure = f"HTTP {response.status_code}"
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
            delay = self.policy.delay(attempt, retry_after)
            if delay is None:
                raise ApiError(f"POST /messages: {failure}, gave up after {attempt} attempt(s)")
            emit(
                "http_retry",
                f"POST /messages for {action.task_id}: {failure}, retry {attempt}/"
                f"{self.policy.max_attempts - 1} in {delay:.2f}s",
                level=logging.WARNING,
                method="POST",
                url="/messages",
                failure=failure,
                attempt=attempt,
                delay=round(delay, 3),
            )
            self.policy.sleep(delay)

    def _delivered(self, action: Action, message_id: str | None, how: str) -> Delivery:
        self.store.mark_sent(action.action_id, message_id)
        verb = "sent" if how == "sent" else "already with the provider, recorded as sent"
        emit(
            "delivery",
            f"{action.task_id}: {action.decision.value} -> {action.target}: {verb} ({message_id})",
            task_id=action.task_id,
            action_id=action.action_id,
            decision=action.decision.value,
            target=action.target,
            status=how,
            message_id=message_id,
        )
        return Delivery(how, action, message_id)


def _message_id(response: httpx2.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data.get("message_id") if isinstance(data, dict) else None
