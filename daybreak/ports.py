"""Boundaries between modules. JSON paths and PayPal error mapping live in paypal.py,
and are filled in only after observing the real Sandbox."""
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from .domain import Reason


@dataclass(frozen=True)
class CaptureFacts:
    capture_id: str
    status: str
    amount_cents: int
    currency: str


RefundStatus = Literal["completed", "pending", "failed_definite", "unknown"]


@dataclass(frozen=True)
class RefundResult:
    """unknown: the outcome could not be established (timeout, cancellation, lost response).
    It must never be treated as a failure, and adapters return it rather than raise."""
    status: RefundStatus
    refund_id: str | None = None
    error_code: str | None = None


class PayPalPort(Protocol):
    async def get_capture(self, capture_id: str) -> CaptureFacts | None: ...
    async def refund(
        self, capture_id: str, amount_cents: int, currency: str, request_id: str
    ) -> RefundResult: ...
    async def get_refund(self, refund_id: str) -> RefundResult: ...


@dataclass(frozen=True)
class Extraction:
    kind: Literal["refund", "question", "other"]
    ambiguous: bool
    summary: str  # shown to the customer; never carries the order, session or amount decision


class ExtractorPort(Protocol):
    async def extract(self, message: str, session_name: str) -> Extraction: ...


OptionKind = Literal["refund", "move", "keep"]


@dataclass(frozen=True)
class Option:
    """Built by code from policy, order and seats. The model can only point at one by id."""
    id: str  # "refund", "keep", or "move:<session id>"
    kind: OptionKind
    label: str
    amount_cents: int | None = None
    starts_at: datetime | None = None


@dataclass(frozen=True)
class AgentTurn:
    kind: Literal["refund", "question", "other"]
    ambiguous: bool
    summary: str  # English, for the seller
    reply: str  # in the customer's language; never authoritative
    language: str  # BCP 47, e.g. "en", "ko"
    suggested_option: str | None = None


class AgentPort(Protocol):
    async def respond(
        self, history: list[tuple[str, str]], session_name: str, options: list[Option]
    ) -> AgentTurn: ...


class Clock(Protocol):
    def now(self) -> datetime: ...  # timezone-aware UTC


@dataclass(frozen=True)
class CaseView:
    """What web.py renders. The three evidence lines stay separate on purpose."""
    request_id: str
    status: str
    reason: Reason
    amount_cents: int
    customer_requested: str
    seller_allowed: str
    paypal_confirmed: str | None
    job_id: str | None
    job_state: str | None
    refund_id: str | None
    actor: Literal["agent", "seller"] | None


@dataclass(frozen=True)
class DraftView:
    draft_id: str
    version: int
    kind: str
    ambiguous: bool
    summary: str
    amount_cents: int  # filled by the server from the order
    session_name: str


@dataclass(frozen=True)
class MorningSummary:
    auto_completed_cents: int
    manual_completed_cents: int
    pending_approval_cents: int
    remaining_delegation_cents: int | None


@dataclass(frozen=True)
class DelegationView:
    id: str
    per_refund_limit_cents: int
    total_limit_cents: int
    held_cents: int
    expires_at: datetime
    stopped_at: datetime | None


@dataclass(frozen=True)
class ConversationView:
    draft_id: str
    version: int
    reply: str
    reply_was_replaced: bool  # the money guard rejected the model's text
    language: str
    options: list[Option]
    suggested_option: str | None
    session_name: str
    messages: list[tuple[str, str]]  # (role, body)


@dataclass(frozen=True)
class TimelineEvent:
    at: datetime
    request_id: str
    kind: str
    detail: str


class ServiceError(Exception):
    """Base for caller errors. Provider failures are not errors: they appear as CaseView states."""


class NotFound(ServiceError): ...  # HTTP 404: also used when the order does not own the resource
class StaleVersion(ServiceError): ...  # HTTP 409: the customer must re-confirm the current version
class InvalidState(ServiceError): ...  # HTTP 409: e.g. approving a request not waiting for approval
class InvalidInput(ServiceError): ...  # HTTP 422


class RefundService(Protocol):
    """Implemented by execute.py. web.py calls these and duplicates no decisions or DB writes.
    order_id and seller authentication are resolved by auth.py before these are called."""
    async def submit_message(self, order_id: str, message: str) -> DraftView: ...
    async def edit_message(self, draft_id: str, order_id: str, message: str) -> DraftView: ...
    async def confirm(self, draft_id: str, draft_version: int, order_id: str) -> CaseView: ...
    async def approve(self, request_id: str) -> CaseView: ...  # seller only
    async def converse(self, order_id: str, message: str, draft_id: str | None = None) -> ConversationView: ...
    async def choose(self, draft_id: str, draft_version: int, order_id: str, option_id: str) -> CaseView: ...
    def timeline(self, limit: int = 100) -> list[TimelineEvent]: ...
    async def start_delegation(
        self, per_refund_limit_cents: int, total_limit_cents: int, expires_at: datetime
    ) -> str: ...
    async def stop_delegation(self, delegation_id: str) -> None: ...
    async def reconcile_job(self, job_id: str) -> None: ...  # seller only; same key and body
    def resume_auto(self) -> None: ...  # seller only; InvalidState while unknown jobs remain
    def auto_paused(self) -> bool: ...
    def latest_delegation(self) -> DelegationView | None: ...
    def get_case(self, request_id: str) -> CaseView | None: ...
    def list_cases(self) -> list[CaseView]: ...
    def morning_summary(self) -> MorningSummary: ...
