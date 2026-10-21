from __future__ import annotations

import pytest

from conftest import Sleeps, no_wait_policy
from followup.api import MessagesApi
from followup.delivery import Outbox
from followup.mock_api import MockBackend
from followup.models import Action, Decision
from followup.retry import CircuitBreaker
from followup.store import Store


def action(n: int = 1) -> Action:
    return Action(
        action_id=f"act-{n}",
        task_id=f"TC-{1000 + n}",
        decision=Decision.FOLLOW_UP_OWNER,
        target="John Smith",
        reason="Overdue by 3 days",
        message=f"Hi John, TC-{1000 + n} is 3 days overdue. Please reply with the status.",
        drafted_by="template",
    )


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "state.db")
    yield store
    store.close()


def outbox(store: Store, backend: MockBackend, sleeps: Sleeps | None = None) -> Outbox:
    policy = no_wait_policy(sleeps)
    return Outbox(store, MessagesApi(backend.client(), policy), policy, CircuitBreaker())


def posts(backend: MockBackend) -> list[str]:
    return [call for call in backend.calls if call.startswith("POST")]


def test_new_action_is_sent_and_recorded(store):
    backend = MockBackend([])

    delivery = outbox(store, backend).deliver(action())

    assert (delivery.status, delivery.message_id) == ("sent", "msg-0001")
    stored = store.get_action("act-1")
    assert (stored.status, stored.attempts, stored.message_id) == ("sent", 1, "msg-0001")


def test_same_action_is_never_sent_twice(store):
    backend = MockBackend([])
    box = outbox(store, backend)

    box.deliver(action())
    again = box.deliver(action())

    assert again.status == "duplicate_suppressed"
    assert len(posts(backend)) == 1


def test_timeout_after_commit_is_reconciled_instead_of_resent(store):
    backend = MockBackend([], faults={"send": ["timeout-after-commit"]})

    delivery = outbox(store, backend).deliver(action())

    assert (delivery.status, delivery.message_id) == ("reconciled", "msg-0001")
    assert backend.calls == ["POST /messages -> 201, lost", "GET /messages/act-1 -> 200"]
    assert len(backend.messages) == 1
    assert store.get_action("act-1").status == "sent"


def test_timeout_before_commit_is_checked_then_retried_with_the_same_key(store):
    sleeps = Sleeps()
    backend = MockBackend([], faults={"send": ["timeout"]})

    delivery = outbox(store, backend, sleeps).deliver(action())

    assert delivery.status == "sent"
    assert backend.calls == [
        "POST /messages -> timeout",
        "GET /messages/act-1 -> 404",
        "POST /messages -> 201",
    ]
    assert len(sleeps) == 1


def test_rate_limit_and_5xx_on_send_are_retried(store):
    sleeps = Sleeps()
    backend = MockBackend([], faults={"send": ["429", "503"]})

    delivery = outbox(store, backend, sleeps).deliver(action())

    assert delivery.status == "sent"
    assert sleeps == [1.0, 0.5]
    assert len(backend.messages) == 1


def test_crash_between_commit_and_bookkeeping_is_recovered_next_run(store, tmp_path):
    log = tmp_path / "delivery_log.jsonl"
    first = MockBackend([], delivery_log=log)
    # The previous run claimed the action and its POST reached the provider,
    # then the process died before recording the result.
    store.claim(action())
    store.start_attempt("act-1")
    first.client().post(
        "/messages",
        json={"to": "John Smith", "task_id": "TC-1001", "body": action().message},
        headers={"Idempotency-Key": "act-1"},
    )

    restarted = MockBackend([], delivery_log=log)
    delivery = outbox(store, restarted).deliver(action())

    assert delivery.status == "reconciled"
    assert posts(restarted) == []
    assert len(log.read_text().splitlines()) == 1


def test_unconfirmed_send_fails_safely_and_finishes_next_run(store):
    backend = MockBackend([], faults={"send": ["timeout-after-commit"], "lookup": ["503"] * 4})

    first = outbox(store, backend).deliver(action())
    second = outbox(store, backend).deliver(action())

    assert first.status == "failed"
    assert store.get_action("act-1").last_error is None  # cleared once sent
    assert second.status == "reconciled"
    assert len(backend.messages) == 1
    assert len(posts(backend)) == 1


def test_rejected_request_is_not_retried(store):
    backend = MockBackend([])
    bad = Action(**{**action().__dict__, "message": ""})

    delivery = outbox(store, backend).deliver(bad)

    assert delivery.status == "failed"
    assert "HTTP 422" in delivery.detail
    assert len(posts(backend)) == 1
    assert store.get_action("act-1").status == "failed"


def test_breaker_defers_the_rest_of_the_run_when_the_provider_is_down(store):
    backend = MockBackend([], faults={"send": ["503"] * 100})
    box = outbox(store, backend)

    statuses = [box.deliver(action(n)).status for n in range(1, 6)]

    assert statuses == ["failed", "failed", "failed", "deferred", "deferred"]
    assert len(posts(backend)) == 3 * 4  # three actions, four attempts each, then silence
    assert store.get_action("act-4").status == "pending"


def test_sent_action_is_never_downgraded(store):
    store.claim(action())
    store.mark_sent("act-1", "msg-0001")

    store.mark_failed("act-1", "late error from a concurrent run")

    assert store.get_action("act-1").status == "sent"


def test_claim_returns_the_first_writer_wins_copy(store):
    store.claim(action())

    stored = store.claim(Action(**{**action().__dict__, "message": "different wording"}))

    assert stored.action.message == action().message
