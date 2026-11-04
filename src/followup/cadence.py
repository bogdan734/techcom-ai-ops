"""Reminder cadence: when an unchanged task is chased again, and when the chasing stops.

A task state that was followed up already gets the same follow-up again every
``remind_every_days`` days, ``max_reminders`` times at most, and then one escalation. Each round
has its own action id, so the outbox still deduplicates every message while a delivered round
does not block the next one.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from .models import Decision, Task, Verdict, action_id
from .rules import Policy, final_escalation
from .store import Store


def follow_up_due(
    store: Store, task: Task, verdict: Verdict, today: date, policy: Policy
) -> tuple[Verdict, str]:
    """The verdict and action id to act on today.

    The current round is the first one that is not delivered yet or was delivered less than
    ``remind_every_days`` days ago: a round that never went out holds back the next one, and
    the days count from its delivery. Once every round was delivered and the last interval has
    passed, the final escalation is due. An escalation is never repeated: it is the top of the
    ladder, and the final one shares its id with the threshold escalation of the same state, so
    only one of the two is ever sent.
    """
    if policy.remind_every_days == 0 or verdict.decision is Decision.ESCALATE:
        return verdict, action_id(task, verdict)
    for repeat in range(policy.max_reminders + 1):
        round_id = action_id(task, verdict, repeat)
        stored = store.get_action(round_id)
        if (
            stored is None
            or stored.status != "sent"
            or (today - stored.sent_on).days < policy.remind_every_days
        ):
            return _round(verdict, repeat, policy), round_id
    final = final_escalation(verdict, policy)
    return final, action_id(task, final)


def _round(verdict: Verdict, repeat: int, policy: Policy) -> Verdict:
    if repeat == 0:
        return verdict
    reason = f"{verdict.reason}, reminder {repeat} of {policy.max_reminders}"
    return replace(verdict, reason=reason)
