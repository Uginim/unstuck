"""Refund service: confirmation, reservation, execution and recovery.

Invariants: one job per capture; reservations count toward limits; the attempt and its key
are committed before any network call; no database transaction spans a network call;
an unknown result keeps its reservation and is only retried with the same key and body.
"""
import asyncio
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .domain import (
    HOLDS_LIMIT,
    Actor,
    ConfirmedRequest,
    Decision,
    Delegation,
    ExistingJob,
    Order,
    Policy,
    Usage,
    decide,
)
from .logbook import Logbook
from .paypal import cents_to_value
from .ports import (
    CaptureFacts,
    CaseView,
    Clock,
    DraftView,
    Extraction,
    ExtractorPort,
    InvalidInput,
    InvalidState,
    MorningSummary,
    NotFound,
    PayPalPort,
    RefundResult,
    StaleVersion,
)

SCHEMA = Path(__file__).with_name("schema.sql")
PAUSE_FLAG = "auto_execution_paused"
STATUS_BY_OUTCOME = {
    "execute": "approved",
    "pending_approval": "pending_approval",
    "rejected_policy": "rejected_policy",
    "needs_review": "needs_review",
    "link_existing": "linked",
}
UNREADABLE = Extraction("other", True, "We could not read this request automatically.")


def _usd(cents: int) -> str:
    return f"${cents_to_value(cents)}"


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _order(row: sqlite3.Row) -> Order:
    return Order(
        row["id"], row["capture_id"], row["amount_cents"], row["currency"], _time(row["session_starts_at"])
    )


def _delegation(row: sqlite3.Row | None) -> Delegation | None:
    if row is None:
        return None
    return Delegation(
        row["id"],
        row["per_refund_limit_cents"],
        row["total_limit_cents"],
        _time(row["expires_at"]),
        _time(row["stopped_at"]),
    )


@dataclass(frozen=True)
class _Send:
    request_id: str
    job_id: str
    attempt_id: str
    capture_id: str
    amount_cents: int
    currency: str
    key: str


class Service:
    def __init__(
        self,
        db_path: str,
        paypal: PayPalPort,
        extractor: ExtractorPort,
        clock: Clock,
        log: Logbook | None = None,
    ):
        self._db = db_path
        self._paypal = paypal
        self._extractor = extractor
        self._clock = clock
        self._log = log or Logbook(None)
        with sqlite3.connect(db_path) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA.read_text())

    @contextmanager
    def _tx(self):
        c = sqlite3.connect(self._db, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    # Customer path

    async def _extract(self, message: str, session_name: str) -> Extraction:
        try:
            result = await self._extractor.extract(message, session_name)
        except Exception as error:  # model failure sends the request to review, never to execution
            self._log.write("extract_error", message=message, error=repr(error))
            return UNREADABLE
        self._log.write("extract", message=message, result=result)
        return result

    def _order_row(self, c: sqlite3.Connection, order_id: str) -> sqlite3.Row:
        row = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise NotFound("order")
        return row

    def _draft_view(self, draft: sqlite3.Row, order: sqlite3.Row) -> DraftView:
        return DraftView(
            draft["id"],
            draft["version"],
            draft["kind"],
            bool(draft["ambiguous"]),
            draft["summary"],
            order["amount_cents"],
            order["session_name"],
        )

    async def submit_message(self, order_id: str, message: str) -> DraftView:
        message = message.strip()
        if not message:
            raise InvalidInput("message")
        with self._tx() as c:
            session = self._order_row(c, order_id)["session_name"]
        extraction = await self._extract(message, session)
        now = self._clock.now().isoformat()
        draft_id = uuid.uuid4().hex
        with self._tx() as c:
            c.execute(
                "INSERT INTO request_drafts (id, order_id, message, kind, ambiguous, summary, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (draft_id, order_id, message, extraction.kind, int(extraction.ambiguous), extraction.summary, now, now),
            )
            draft = c.execute("SELECT * FROM request_drafts WHERE id=?", (draft_id,)).fetchone()
            return self._draft_view(draft, self._order_row(c, order_id))

    async def edit_message(self, draft_id: str, order_id: str, message: str) -> DraftView:
        message = message.strip()
        if not message:
            raise InvalidInput("message")
        with self._tx() as c:
            session = self._order_row(c, order_id)["session_name"]
        extraction = await self._extract(message, session)
        with self._tx() as c:
            updated = c.execute(
                "UPDATE request_drafts SET message=?, kind=?, ambiguous=?, summary=?, version=version+1, updated_at=?"
                " WHERE id=? AND order_id=?",
                (message, extraction.kind, int(extraction.ambiguous), extraction.summary,
                 self._clock.now().isoformat(), draft_id, order_id),
            )
            if updated.rowcount != 1:
                raise NotFound("draft")
            draft = c.execute("SELECT * FROM request_drafts WHERE id=?", (draft_id,)).fetchone()
            return self._draft_view(draft, self._order_row(c, order_id))

    async def confirm(self, draft_id: str, draft_version: int, order_id: str) -> CaseView:
        with self._tx() as c:
            capture_id = self._order_row(c, order_id)["capture_id"]
        facts = await self._paypal.get_capture(capture_id)
        request_id, send = await asyncio.to_thread(self._confirm_tx, draft_id, draft_version, order_id, facts)
        if send:
            await self._run(send)
        return self.get_case(request_id)

    def _confirm_tx(self, draft_id, draft_version, order_id, facts) -> tuple[str, _Send | None]:
        with self._tx() as c:
            draft = c.execute(
                "SELECT * FROM request_drafts WHERE id=? AND order_id=?", (draft_id, order_id)
            ).fetchone()
            if draft is None:
                raise NotFound("draft")
            if draft["version"] != draft_version:
                raise StaleVersion("draft changed")
            replay = c.execute(
                "SELECT id FROM confirmed_requests WHERE draft_id=? AND draft_version=?", (draft_id, draft_version)
            ).fetchone()
            if replay:
                return replay["id"], None

            order_row = self._order_row(c, order_id)
            now = self._clock.now()
            request_id = uuid.uuid4().hex
            req = ConfirmedRequest(
                request_id, draft_version, draft["kind"], bool(draft["ambiguous"]), order_row["amount_cents"], now
            )
            delegation_row = c.execute("SELECT * FROM delegations ORDER BY started_at DESC LIMIT 1").fetchone()
            policy_version = delegation_row["policy_version"] if delegation_row else self._latest_policy(c)
            decision = self._decide(c, req, order_row, policy_version, delegation_row, facts, now, "agent")
            c.execute(
                "INSERT INTO confirmed_requests (id, draft_id, draft_version, order_id, kind, ambiguous, amount_cents,"
                " received_at, policy_version, delegation_id, status, reason, job_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, draft_id, draft_version, order_id, req.kind, int(req.ambiguous), req.amount_cents,
                 now.isoformat(), policy_version, delegation_row["id"] if delegation_row else None,
                 STATUS_BY_OUTCOME[decision.outcome], decision.reason, decision.existing_job_id),
            )
            delegation_id = delegation_row["id"] if delegation_row else None
            send = self._reserve(c, request_id, order_row, decision, "agent", delegation_id, now)
            self._log.write("decision", request_id=request_id, actor="agent", decision=decision)
            return request_id, send

    def _latest_policy(self, c: sqlite3.Connection) -> int:
        row = c.execute("SELECT MAX(version) AS v FROM policies").fetchone()
        if row["v"] is None:
            raise InvalidState("no policy")
        return row["v"]

    def _decide(self, c, req, order_row, policy_version, delegation_row, facts, now, actor: Actor) -> Decision:
        order = _order(order_row)
        policy_row = c.execute("SELECT * FROM policies WHERE version=?", (policy_version,)).fetchone()
        policy = Policy(policy_row["version"], policy_row["full_refund_cutoff_hours"])
        held = 0
        if delegation_row:
            marks = ",".join("?" * len(HOLDS_LIMIT))
            held = c.execute(
                f"SELECT COALESCE(SUM(amount_cents),0) AS s FROM refund_jobs WHERE actor='agent'"
                f" AND delegation_id=? AND state IN ({marks})",
                (delegation_row["id"], *sorted(HOLDS_LIMIT)),
            ).fetchone()["s"]
        job = c.execute("SELECT * FROM refund_jobs WHERE capture_id=?", (order.capture_id,)).fetchone()
        existing = ExistingJob(job["id"], job["state"], job["refund_id"]) if job else None
        decision = decide(
            req, order, policy, _delegation(delegation_row), Usage(held), existing, evaluated_at=now, actor=actor
        )
        if decision.outcome != "execute":
            return decision
        if not self._facts_match(facts, order):
            return Decision("needs_review", "insufficient_order_facts", decision.amount_cents)
        if actor == "agent" and self._paused(c):
            return Decision("pending_approval", "delegation_stopped", decision.amount_cents)
        return decision

    @staticmethod
    def _facts_match(facts: CaptureFacts | None, order: Order) -> bool:
        return (
            facts is not None
            and facts.status == "COMPLETED"
            and facts.amount_cents == order.amount_cents
            and facts.currency == order.currency
        )

    def _paused(self, c: sqlite3.Connection) -> bool:
        row = c.execute("SELECT value FROM system_flags WHERE key=?", (PAUSE_FLAG,)).fetchone()
        return bool(row and row["value"] == "1")

    def _reserve(self, c, request_id, order_row, decision, actor, delegation_id, now) -> _Send | None:
        if decision.outcome != "execute":
            return None
        stamp = now.isoformat()
        capture_id = order_row["capture_id"]
        job = c.execute("SELECT * FROM refund_jobs WHERE capture_id=?", (capture_id,)).fetchone()
        if job is None:
            job_id = uuid.uuid4().hex
            c.execute(
                "INSERT INTO refund_jobs (id, capture_id, request_id, actor, delegation_id, amount_cents, state,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,'reserved',?,?)",
                (job_id, capture_id, request_id, actor, delegation_id, decision.amount_cents, stamp, stamp),
            )
        else:  # decide() only allows this for a definite earlier failure
            job_id = job["id"]
            updated = c.execute(
                "UPDATE refund_jobs SET state='reserved', request_id=?, actor=?, delegation_id=?, amount_cents=?,"
                " refund_id=NULL, updated_at=? WHERE id=? AND state='failed'",
                (request_id, actor, delegation_id, decision.amount_cents, stamp, job_id),
            )
            if updated.rowcount != 1:
                raise InvalidState("job is not retryable")
        number = c.execute("SELECT COUNT(*) AS n FROM refund_attempts WHERE job_id=?", (job_id,)).fetchone()["n"] + 1
        attempt_id, key = uuid.uuid4().hex, f"daybreak-{uuid.uuid4().hex}"
        c.execute(
            "INSERT INTO refund_attempts (id, job_id, attempt_no, paypal_request_id, amount_cents, started_at)"
            " VALUES (?,?,?,?,?,?)",
            (attempt_id, job_id, number, key, decision.amount_cents, stamp),
        )
        c.execute("UPDATE confirmed_requests SET job_id=? WHERE id=?", (job_id, request_id))
        return _Send(request_id, job_id, attempt_id, capture_id, decision.amount_cents, order_row["currency"], key)

    # Execution

    async def _run(self, send: _Send) -> None:
        await asyncio.to_thread(self._set_state, send.job_id, "executing", ("reserved", "unknown"))
        try:
            result = await self._paypal.refund(send.capture_id, send.amount_cents, send.currency, send.key)
        except asyncio.CancelledError:
            self._record(send, RefundResult("unknown", error_code="cancelled"))
            raise
        except Exception as error:
            result = RefundResult("unknown", error_code=type(error).__name__)
        await asyncio.to_thread(self._record, send, result)
        self._log.write("refund_result", request_id=send.request_id, job_id=send.job_id, key=send.key, result=result)
        if result.status == "completed" and result.refund_id:
            confirmed = await self._paypal.get_refund(result.refund_id)
            self._log.write("refund_requery", job_id=send.job_id, result=confirmed)

    def _set_state(self, job_id: str, state: str, allowed: tuple[str, ...]) -> None:
        marks = ",".join("?" * len(allowed))
        with self._tx() as c:
            c.execute(
                f"UPDATE refund_jobs SET state=?, updated_at=? WHERE id=? AND state IN ({marks})",
                (state, self._clock.now().isoformat(), job_id, *allowed),
            )

    def _record(self, send: _Send, result: RefundResult) -> None:
        state = {
            "completed": "completed",
            "pending": "paypal_pending",
            "failed_definite": "failed",
            "unknown": "unknown",
        }[result.status]
        now = self._clock.now().isoformat()
        with self._tx() as c:
            c.execute(
                "UPDATE refund_attempts SET result=?, refund_id=?, error_code=?, finished_at=? WHERE id=?",
                (result.status, result.refund_id, result.error_code, now, send.attempt_id),
            )
            c.execute(
                "UPDATE refund_jobs SET state=?, refund_id=COALESCE(?, refund_id), updated_at=? WHERE id=?",
                (state, result.refund_id, now, send.job_id),
            )

    # Seller path

    async def approve(self, request_id: str) -> CaseView:
        with self._tx() as c:
            row = c.execute("SELECT order_id FROM confirmed_requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise NotFound("request")
            capture_id = self._order_row(c, row["order_id"])["capture_id"]
        facts = await self._paypal.get_capture(capture_id)
        send = await asyncio.to_thread(self._approve_tx, request_id, facts)
        if send:
            await self._run(send)
        return self.get_case(request_id)

    def _approve_tx(self, request_id: str, facts) -> _Send | None:
        with self._tx() as c:
            row = c.execute("SELECT * FROM confirmed_requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise NotFound("request")
            if row["status"] != "pending_approval":
                raise InvalidState(row["status"])
            draft = c.execute("SELECT version FROM request_drafts WHERE id=?", (row["draft_id"],)).fetchone()
            if draft["version"] != row["draft_version"]:
                raise StaleVersion("draft changed after confirmation")
            order_row = self._order_row(c, row["order_id"])
            now = self._clock.now()
            req = ConfirmedRequest(
                request_id, row["draft_version"], row["kind"], bool(row["ambiguous"]),
                row["amount_cents"], _time(row["received_at"]),
            )
            decision = self._decide(c, req, order_row, row["policy_version"], None, facts, now, "seller")
            c.execute(
                "UPDATE confirmed_requests SET status=?, reason=?, job_id=COALESCE(?, job_id) WHERE id=?",
                (STATUS_BY_OUTCOME[decision.outcome], decision.reason, decision.existing_job_id, request_id),
            )
            self._log.write("decision", request_id=request_id, actor="seller", decision=decision)
            return self._reserve(c, request_id, order_row, decision, "seller", None, now)

    async def start_delegation(
        self, per_refund_limit_cents: int, total_limit_cents: int, expires_at: datetime
    ) -> str:
        now = self._clock.now()
        if per_refund_limit_cents < 0 or total_limit_cents < 0 or expires_at.tzinfo is None or expires_at <= now:
            raise InvalidInput("delegation")
        delegation_id = uuid.uuid4().hex
        with self._tx() as c:
            c.execute(
                "UPDATE delegations SET stopped_at=? WHERE stopped_at IS NULL", (now.isoformat(),)
            )
            c.execute(
                "INSERT INTO delegations (id, per_refund_limit_cents, total_limit_cents, policy_version, started_at,"
                " expires_at) VALUES (?,?,?,?,?,?)",
                (delegation_id, per_refund_limit_cents, total_limit_cents, self._latest_policy(c),
                 now.isoformat(), expires_at.isoformat()),
            )
        return delegation_id

    async def stop_delegation(self, delegation_id: str) -> None:
        with self._tx() as c:
            updated = c.execute(
                "UPDATE delegations SET stopped_at=COALESCE(stopped_at, ?) WHERE id=?",
                (self._clock.now().isoformat(), delegation_id),
            )
            if updated.rowcount != 1:
                raise NotFound("delegation")

    # Recovery

    def recover_on_start(self) -> int:
        """Nothing is resent here. Jobs interrupted mid-flight become unknown and pause auto-execution."""
        with self._tx() as c:
            now = self._clock.now().isoformat()
            c.execute(
                "UPDATE refund_jobs SET state='unknown', updated_at=? WHERE state IN ('reserved','executing')", (now,)
            )
            open_jobs = c.execute("SELECT COUNT(*) AS n FROM refund_jobs WHERE state='unknown'").fetchone()["n"]
            if open_jobs:
                c.execute("INSERT OR REPLACE INTO system_flags (key, value) VALUES (?, '1')", (PAUSE_FLAG,))
            return open_jobs

    async def reconcile_job(self, job_id: str) -> None:
        with self._tx() as c:
            job = c.execute("SELECT * FROM refund_jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise NotFound("job")
            attempt = c.execute(
                "SELECT * FROM refund_attempts WHERE job_id=? ORDER BY attempt_no DESC LIMIT 1", (job_id,)
            ).fetchone()
            order_row = c.execute("SELECT * FROM orders WHERE capture_id=?", (job["capture_id"],)).fetchone()
        send = _Send(
            job["request_id"], job_id, attempt["id"], job["capture_id"], attempt["amount_cents"],
            order_row["currency"], attempt["paypal_request_id"],
        )
        if job["state"] == "unknown":
            await self._run(send)  # same key and body
        elif job["state"] == "paypal_pending" and job["refund_id"]:
            result = await self._paypal.get_refund(job["refund_id"])
            if result.status != "unknown":
                await asyncio.to_thread(self._record, send, result)
        else:
            raise InvalidState(job["state"])
        with self._tx() as c:
            remaining = c.execute("SELECT COUNT(*) AS n FROM refund_jobs WHERE state='unknown'").fetchone()["n"]
            if not remaining:
                c.execute("DELETE FROM system_flags WHERE key=?", (PAUSE_FLAG,))

    # Views

    def get_case(self, request_id: str) -> CaseView:
        with self._tx() as c:
            row = self._case_row(c, "WHERE r.id=?", (request_id,)).fetchone()
            if row is None:
                raise NotFound("request")
            return self._case_view(c, row)

    def list_cases(self) -> list[CaseView]:
        with self._tx() as c:
            rows = self._case_row(c, "ORDER BY r.received_at DESC", ()).fetchall()
            return [self._case_view(c, row) for row in rows]

    def _case_row(self, c, tail: str, args: tuple):
        return c.execute(
            "SELECT r.*, d.message, d.summary, o.session_name, j.state AS job_state, j.refund_id, j.actor"
            " FROM confirmed_requests r JOIN request_drafts d ON d.id=r.draft_id"
            " JOIN orders o ON o.id=r.order_id LEFT JOIN refund_jobs j ON j.id=r.job_id " + tail,
            args,
        )

    def _case_view(self, c, row: sqlite3.Row) -> CaseView:
        policy = c.execute("SELECT * FROM policies WHERE version=?", (row["policy_version"],)).fetchone()
        allowed = f"Policy v{policy['version']}: full refund until {policy['full_refund_cutoff_hours']}h before the session."
        if row["delegation_id"]:
            d = c.execute("SELECT * FROM delegations WHERE id=?", (row["delegation_id"],)).fetchone()
            allowed += (
                f" Delegation: {_usd(d['per_refund_limit_cents'])} per refund,"
                f" {_usd(d['total_limit_cents'])} total, until {d['expires_at']}."
            )
        else:
            allowed += " No delegation."
        confirmed = None
        if row["job_state"] in ("completed", "paypal_pending") and row["refund_id"]:
            confirmed = f"Refund {row['refund_id']}: {row['job_state']}"
        return CaseView(
            request_id=row["id"],
            status=row["status"],
            reason=row["reason"],
            amount_cents=row["amount_cents"],
            customer_requested=f"{row['summary']} ({_usd(row['amount_cents'])}, {row['session_name']})",
            seller_allowed=allowed,
            paypal_confirmed=confirmed,
            job_id=row["job_id"],
            job_state=row["job_state"],
            refund_id=row["refund_id"],
            actor=row["actor"],
        )

    def morning_summary(self) -> MorningSummary:
        with self._tx() as c:
            total = lambda actor: c.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS s FROM refund_jobs WHERE actor=? AND state='completed'",
                (actor,),
            ).fetchone()["s"]
            pending = c.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS s FROM confirmed_requests WHERE status='pending_approval'"
            ).fetchone()["s"]
            d = c.execute("SELECT * FROM delegations ORDER BY started_at DESC LIMIT 1").fetchone()
            remaining = None
            if d:
                marks = ",".join("?" * len(HOLDS_LIMIT))
                held = c.execute(
                    f"SELECT COALESCE(SUM(amount_cents),0) AS s FROM refund_jobs WHERE actor='agent'"
                    f" AND delegation_id=? AND state IN ({marks})",
                    (d["id"], *sorted(HOLDS_LIMIT)),
                ).fetchone()["s"]
                remaining = d["total_limit_cents"] - held
            return MorningSummary(total("agent"), total("seller"), pending, remaining)
