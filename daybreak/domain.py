"""Refund decisions. Pure: no I/O and no reading the clock; every input is a value."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

Actor = Literal["agent", "seller"]
Outcome = Literal["execute", "pending_approval", "rejected_policy", "needs_review", "link_existing"]
Reason = Literal[
    "ok",
    "ambiguous_request",
    "not_refund_request",
    "insufficient_order_facts",
    "already_refunded",
    "in_progress",
    "result_unconfirmed",
    "outside_window",
    "no_delegation",
    "delegation_stopped",
    "delegation_expired",
    "over_per_refund",
    "over_total",
]
JobState = Literal["reserved", "executing", "paypal_pending", "completed", "failed", "unknown"]

# States whose amount counts against a delegation. unknown keeps its reservation.
HOLDS_LIMIT: frozenset[str] = frozenset(
    {"reserved", "executing", "paypal_pending", "unknown", "completed"}
)
SUPPORTED_CURRENCY = "USD"


@dataclass(frozen=True)
class ConfirmedRequest:
    id: str
    draft_version: int
    kind: Literal["refund", "question", "other"]
    ambiguous: bool
    amount_cents: int
    received_at: datetime  # server receipt of the confirmation; decides the refund window


@dataclass(frozen=True)
class Order:
    id: str
    capture_id: str
    amount_cents: int
    currency: str
    session_starts_at: datetime


@dataclass(frozen=True)
class Policy:
    version: int
    full_refund_cutoff_hours: int


@dataclass(frozen=True)
class Delegation:
    id: str
    per_refund_limit_cents: int
    total_limit_cents: int
    expires_at: datetime
    stopped_at: datetime | None


@dataclass(frozen=True)
class Usage:
    """Agent-executed amounts under this delegation. Seller approvals are not included."""
    held_cents: int  # completed + reserved + executing + paypal_pending + unknown


@dataclass(frozen=True)
class ExistingJob:
    id: str
    state: JobState
    refund_id: str | None


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reason: Reason
    amount_cents: int
    existing_job_id: str | None = None


def decide(
    req: ConfirmedRequest,
    order: Order,
    policy: Policy,
    delegation: Delegation | None,
    usage: Usage,
    existing: ExistingJob | None,
    *,
    evaluated_at: datetime,
    actor: Actor,
) -> Decision:
    """Order: evidence, existing job, policy, authority.

    Policy precedes authority, so pending_approval always means "eligible, but not
    authorized to run alone". A seller approval re-runs this and skips authority only.
    actor must be set by the authenticated server, never from client input.
    A definite earlier failure (state "failed") does not block re-evaluation.
    """
    amount = req.amount_cents

    if req.kind != "refund":
        return Decision("needs_review", "not_refund_request", amount)
    if req.ambiguous:
        return Decision("needs_review", "ambiguous_request", amount)
    if order.currency != SUPPORTED_CURRENCY or amount <= 0 or amount != order.amount_cents:
        return Decision("needs_review", "insufficient_order_facts", amount)

    if existing is not None:
        if existing.state == "completed":
            return Decision("link_existing", "already_refunded", amount, existing.id)
        if existing.state == "unknown":
            return Decision("link_existing", "result_unconfirmed", amount, existing.id)
        if existing.state != "failed":
            return Decision("link_existing", "in_progress", amount, existing.id)

    # Exactly at the cutoff is still inside the window.
    cutoff = order.session_starts_at - timedelta(hours=policy.full_refund_cutoff_hours)
    if req.received_at > cutoff:
        return Decision("rejected_policy", "outside_window", amount)

    if actor == "seller":
        return Decision("execute", "ok", amount)

    if delegation is None:
        return Decision("pending_approval", "no_delegation", amount)
    if delegation.stopped_at is not None and delegation.stopped_at <= evaluated_at:
        return Decision("pending_approval", "delegation_stopped", amount)
    if evaluated_at >= delegation.expires_at:
        return Decision("pending_approval", "delegation_expired", amount)
    if amount > delegation.per_refund_limit_cents:
        return Decision("pending_approval", "over_per_refund", amount)
    if usage.held_cents + amount > delegation.total_limit_cents:
        return Decision("pending_approval", "over_total", amount)
    return Decision("execute", "ok", amount)
