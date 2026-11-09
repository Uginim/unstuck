"""Sales conversation, stock holds and PayPal checkout. Code sets every price and line.

Order of operations for money: the hold and the request ids are committed before any network
call, no transaction spans a network call, and an unknown result keeps the hold and retries
only with the same request id.
"""
import asyncio
import json
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from daybreak.logbook import Logbook

from .domain import Draft, Line, Product, build_offers

SCHEMA = Path(__file__).with_name("schema.sql")
OFFER_TTL = timedelta(minutes=30)
HOLD_TTL = timedelta(minutes=15)
CURRENCY = "USD"
FALLBACK_REPLIES = {
    "ko": "주문하실 수 있는 안을 아래 카드에 정리했습니다. 금액은 카드에 표시된 값이 기준입니다.",
    "en": "Your options are on the cards below. The amounts on the cards are the ones that apply.",
}
# Prices live on code-built cards only. The reply may not talk about money at all, so a checker
# never has to decide whether a number is the right price, currency or role.
NO_MONEY_TALK = re.compile(
    r"[$€£₩¥]|\b(?:usd|eur|gbp|krw|jpy|cad|aud)\b|\b(?:dollars?|euros?|pounds?|bucks?|cents?|free|discounts?)\b"
    r"|달러|유로|파운드|센트|무료|공짜|할인|\d\s*%|(?:\d|[일이삼사오육칠팔구십백천만억])\s*(?:원|불|엔)"
    r"|(?:total|price|cost|총액|합계|가격|금액)\D{0,12}\d",
    re.IGNORECASE,
)
LIVE_HOLD = ("held", "awaiting_payment", "capturing", "unknown", "paid")


class NotFound(Exception): ...
class InvalidState(Exception): ...
class OutOfStock(Exception): ...


@dataclass(frozen=True)
class Interpretation:
    product_id: str | None
    qty: int | None
    summary: str
    language: str = "en"
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micro_usd: int = 0


@dataclass(frozen=True)
class Composition:
    reply: str
    suggested_offer: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micro_usd: int = 0


@dataclass(frozen=True)
class OfferView:
    id: str
    kind: str
    lines: tuple[Line, ...]
    total_cents: int
    expires_at: datetime


class SalesAgent(Protocol):
    async def interpret(self, history: list[tuple[str, str]], catalog: list[Product]) -> Interpretation: ...
    async def compose(self, history: list[tuple[str, str]], offers: list[OfferView], language: str) -> Composition: ...


@dataclass(frozen=True)
class ConversationView:
    id: str
    reply: str
    reply_was_replaced: bool
    language: str
    offers: tuple[OfferView, ...]
    suggested_offer: str | None
    messages: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class CheckoutView:
    id: str
    offer_id: str
    state: str
    total_cents: int
    lines: tuple[Line, ...]
    approve_url: str | None
    paypal_order_id: str | None
    capture_id: str | None
    hold_expires_at: datetime
    error_code: str | None


def _fallback(language: str) -> str:
    return FALLBACK_REPLIES["ko" if str(language).lower().startswith("ko") else "en"]


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _lines(raw: str) -> tuple[Line, ...]:
    return tuple(Line(**item) for item in json.loads(raw))


class ShopService:
    def __init__(self, db_path: str, paypal, agent: SalesAgent, clock, log: Logbook | None = None,
                 return_url: str = "http://localhost:8000/paid", cancel_url: str = "http://localhost:8000/cancel"):
        self._db, self._paypal, self._agent, self._clock = db_path, paypal, agent, clock
        self._log = log or Logbook(None)
        self._return_url, self._cancel_url = return_url, cancel_url
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

    def _event(self, c, checkout_id, kind, detail) -> None:
        c.execute("INSERT INTO events (checkout_id, at, kind, detail) VALUES (?,?,?,?)",
                  (checkout_id, self._clock.now().isoformat(), kind, detail))

    # Catalog

    def catalog(self) -> list[Product]:
        with self._tx() as c:
            return [Product(r["id"], r["name"], r["unit_cents"], r["stock"])
                    for r in c.execute("SELECT * FROM products ORDER BY name")]

    def _drafts(self, c, product_id: str, qty: int) -> list[Draft]:
        row = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if row is None:
            return []
        subs = c.execute(
            "SELECT p.* FROM substitutes s JOIN products p ON p.id=s.substitute_id"
            " WHERE s.product_id=? ORDER BY p.unit_cents", (product_id,),
        ).fetchall()
        product = lambda r: Product(r["id"], r["name"], r["unit_cents"], r["stock"])
        return build_offers(product(row), qty, [product(r) for r in subs])

    # Conversation

    async def message(self, text: str, conversation_id: str | None = None) -> ConversationView:
        text = text.strip()
        if not text:
            raise ValueError("message")
        now = self._clock.now()
        with self._tx() as c:
            if conversation_id is None:
                conversation_id = uuid.uuid4().hex
                c.execute("INSERT INTO conversations VALUES (?,?)", (conversation_id, now.isoformat()))
            elif not c.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone():
                raise NotFound("conversation")
            history = [(r["role"], r["body"]) for r in c.execute(
                "SELECT role, body FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,))]
            c.execute("INSERT INTO messages (conversation_id, role, body, at) VALUES (?,?,?,?)",
                      (conversation_id, "customer", text, now.isoformat()))
        history.append(("customer", text))
        catalog = self.catalog()
        turn = await self._safe_interpret(history, catalog)

        offers: list[OfferView] = []
        with self._tx() as c:
            if turn.product_id and type(turn.qty) is int and 0 < turn.qty <= 10_000:
                expires = now + OFFER_TTL
                for draft in self._drafts(c, turn.product_id, turn.qty):
                    offer_id = uuid.uuid4().hex
                    c.execute("INSERT INTO offers VALUES (?,?,?,?,?,?)", (
                        offer_id, conversation_id, draft.kind,
                        json.dumps([line.__dict__ for line in draft.lines]), draft.total_cents, expires.isoformat(),
                    ))
                    offers.append(OfferView(offer_id, draft.kind, draft.lines, draft.total_cents, expires))
        composed = await self._safe_compose(history, offers, turn.language)
        reply, replaced = self._guard(composed.reply, turn.language)
        suggested = composed.suggested_offer if composed.suggested_offer in {o.id for o in offers} else None
        self._log.write("sales_turn", conversation_id=conversation_id, message=text, turn=turn,
                        composed=composed, shown=reply, replaced=replaced, offers=offers)
        stamp = self._clock.now().isoformat()
        with self._tx() as c:
            c.execute("INSERT INTO messages (conversation_id, role, body, at) VALUES (?,?,?,?)",
                      (conversation_id, "agent", reply, stamp))
            for usage in (turn, composed):
                if usage.model:
                    c.execute("INSERT INTO model_usage (conversation_id, model, input_tokens, output_tokens,"
                              " cost_micro_usd, at) VALUES (?,?,?,?,?,?)",
                              (conversation_id, usage.model, usage.input_tokens, usage.output_tokens,
                               usage.cost_micro_usd, stamp))
            messages = tuple((r["role"], r["body"]) for r in c.execute(
                "SELECT role, body FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,)))
        return ConversationView(conversation_id, reply, replaced, turn.language, tuple(offers), suggested, messages)

    async def _safe_interpret(self, history, catalog) -> Interpretation:
        try:
            turn = await self._agent.interpret(history, catalog)
        except Exception as error:  # A model failure must not take the shop down.
            self._log.write("agent_error", phase="interpret", error=repr(error))
            return Interpretation(None, None, "unreadable")
        if turn.product_id not in {p.id for p in catalog}:
            return Interpretation(None, None, turn.summary, turn.language, turn.model, turn.input_tokens,
                                  turn.output_tokens, turn.cost_micro_usd)
        return turn

    async def _safe_compose(self, history, offers, language) -> Composition:
        try:
            return await self._agent.compose(history, offers, language)
        except Exception as error:
            self._log.write("agent_error", phase="compose", error=repr(error))
            return Composition(_fallback(language))

    @staticmethod
    def _guard(reply: str, language: str) -> tuple[str, bool]:
        if NO_MONEY_TALK.search(unicodedata.normalize("NFKC", reply)):
            return _fallback(language), True
        return reply, False

    # Checkout

    async def choose(self, offer_id: str) -> CheckoutView:
        checkout_id = await asyncio.to_thread(self._hold, offer_id)
        return await self.ensure_order(checkout_id)

    def _hold(self, offer_id: str) -> str:
        now = self._clock.now()
        with self._tx() as c:
            offer = c.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
            if offer is None:
                raise NotFound("offer")
            existing = c.execute("SELECT id FROM checkouts WHERE offer_id=?", (offer_id,)).fetchone()
            if existing:
                return existing["id"]
            if _time(offer["expires_at"]) <= now:
                raise InvalidState("offer expired")
            for line in _lines(offer["lines"]):
                taken = c.execute("UPDATE products SET stock=stock-? WHERE id=? AND stock>=?",
                                  (line.qty, line.product_id, line.qty))
                if taken.rowcount != 1:
                    raise OutOfStock(line.product_id)  # Rollback releases earlier lines too.
            checkout_id = uuid.uuid4().hex
            c.execute(
                "INSERT INTO checkouts (id, offer_id, state, total_cents, hold_expires_at, create_request_id,"
                " capture_request_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (checkout_id, offer_id, "held", offer["total_cents"], (now + HOLD_TTL).isoformat(),
                 f"shop-create-{uuid.uuid4().hex}", f"shop-capture-{uuid.uuid4().hex}",
                 now.isoformat(), now.isoformat()),
            )
            self._event(c, checkout_id, "held", f"{offer['kind']} {offer['total_cents']}")
            return checkout_id

    def _row(self, c, checkout_id: str) -> sqlite3.Row:
        row = c.execute("SELECT * FROM checkouts WHERE id=?", (checkout_id,)).fetchone()
        if row is None:
            raise NotFound("checkout")
        return row

    def _set(self, c, checkout_id: str, state: str, **fields) -> None:
        names = "".join(f", {name}=COALESCE(?, {name})" for name in fields)
        c.execute(f"UPDATE checkouts SET state=?, updated_at=?{names} WHERE id=?",
                  (state, self._clock.now().isoformat(), *fields.values(), checkout_id))

    def _release(self, c, row: sqlite3.Row, state: str, reason: str) -> None:
        """Only called from states where no money can have moved."""
        offer = c.execute("SELECT lines FROM offers WHERE id=?", (row["offer_id"],)).fetchone()
        for line in _lines(offer["lines"]):
            c.execute("UPDATE products SET stock=stock+? WHERE id=?", (line.qty, line.product_id))
        self._set(c, row["id"], state, error_code=reason)
        self._event(c, row["id"], state, reason)

    async def ensure_order(self, checkout_id: str) -> CheckoutView:
        with self._tx() as c:
            row = self._row(c, checkout_id)
        if row["state"] != "held":
            return self.get_checkout(checkout_id)
        result = await self._paypal.create_order(
            row["total_cents"], CURRENCY, row["create_request_id"], row["id"], self._return_url, self._cancel_url,
        )
        with self._tx() as c:
            row = self._row(c, checkout_id)
            if row["state"] == "held":
                if result.status in ("created", "approved"):
                    self._set(c, checkout_id, "awaiting_payment", paypal_order_id=result.order_id,
                              approve_url=result.approve_url)
                    self._event(c, checkout_id, "order_created", result.order_id or "")
                elif result.status == "failed_definite":
                    self._release(c, row, "failed", result.error_code or "order_failed")
                else:  # No payer can have approved an order we never saw; safe to retry with the same id.
                    self._set(c, checkout_id, "held", error_code=result.error_code)
        return self.get_checkout(checkout_id)

    async def capture(self, checkout_id: str) -> CheckoutView:
        """Called when the buyer returns from PayPal. Only the state swap wins the right to send."""
        with self._tx() as c:
            row = self._row(c, checkout_id)
            if row["state"] != "awaiting_payment":
                return self._view(c, row)
            if _time(row["hold_expires_at"]) <= self._clock.now():
                self._release(c, row, "expired", "hold_expired")
                return self._view(c, self._row(c, checkout_id))
            self._set(c, checkout_id, "capturing")
        return await self._send_capture(checkout_id)

    async def _send_capture(self, checkout_id: str) -> CheckoutView:
        with self._tx() as c:
            row = self._row(c, checkout_id)
        result = await self._paypal.capture_order(row["paypal_order_id"], row["capture_request_id"])
        if result.status == "paid":  # A capture response is a claim; the order read is the fact.
            fact = await self._paypal.get_order(row["paypal_order_id"])
            result = fact if fact.status == "paid" and fact.capture_id == result.capture_id else replace(
                result, status="unknown", error_code="paid_unconfirmed")
        await asyncio.to_thread(self._apply_capture, checkout_id, result)
        return self.get_checkout(checkout_id)

    def _apply_capture(self, checkout_id: str, result) -> None:
        with self._tx() as c:
            row = self._row(c, checkout_id)
            if row["state"] not in ("capturing", "unknown"):
                return
            if result.status == "paid":
                if result.amount_cents != row["total_cents"]:
                    # Money moved in an amount we did not offer. A person must look at this.
                    self._set(c, checkout_id, "unknown", capture_id=result.capture_id,
                              error_code="amount_mismatch")
                    self._event(c, checkout_id, "amount_mismatch", str(result.amount_cents))
                    return
                self._set(c, checkout_id, "paid", capture_id=result.capture_id)
                self._event(c, checkout_id, "paid", result.capture_id or "")
            elif result.status == "failed_definite" and result.error_code in ("ORDER_NOT_APPROVED",):
                self._set(c, checkout_id, "awaiting_payment", error_code=result.error_code)
            elif result.status == "failed_definite":
                self._release(c, row, "failed", result.error_code or "capture_failed")
            else:
                self._set(c, checkout_id, "unknown", error_code=result.error_code)
                self._event(c, checkout_id, "unknown", result.error_code or result.status)

    async def reconcile(self, checkout_id: str) -> CheckoutView:
        """Read PayPal first. Resend only with the same request id, and only if not yet captured."""
        with self._tx() as c:
            row = self._row(c, checkout_id)
        if row["state"] != "unknown" or row["error_code"] == "amount_mismatch":
            return self.get_checkout(checkout_id)
        fact = await self._paypal.get_order(row["paypal_order_id"])
        if fact.status == "paid":
            await asyncio.to_thread(self._apply_capture, checkout_id, fact)
        elif fact.status == "approved":
            return await self._send_capture(checkout_id)
        return self.get_checkout(checkout_id)

    async def expire_holds(self) -> int:
        """A hold only ends after PayPal confirms the order was not approved or paid."""
        now = self._clock.now().isoformat()
        with self._tx() as c:
            rows = c.execute(
                "SELECT * FROM checkouts WHERE state IN ('held','awaiting_payment') AND hold_expires_at<=?", (now,),
            ).fetchall()
        expired = 0
        for row in rows:
            fact = await self._paypal.get_order(row["paypal_order_id"]) if row["paypal_order_id"] else None
            if fact and fact.status in ("approved", "paid", "pending", "unknown"):
                continue
            with self._tx() as c:
                current = self._row(c, row["id"])
                if current["state"] in ("held", "awaiting_payment"):
                    self._release(c, current, "expired", "hold_expired")
                    expired += 1
        return expired

    def recover_on_start(self) -> None:
        with self._tx() as c:
            for row in c.execute("SELECT id FROM checkouts WHERE state='capturing'").fetchall():
                self._set(c, row["id"], "unknown", error_code="restart_during_capture")
                self._event(c, row["id"], "unknown", "restart_during_capture")

    # Views

    def _view(self, c, row: sqlite3.Row) -> CheckoutView:
        offer = c.execute("SELECT lines FROM offers WHERE id=?", (row["offer_id"],)).fetchone()
        return CheckoutView(row["id"], row["offer_id"], row["state"], row["total_cents"], _lines(offer["lines"]),
                            row["approve_url"], row["paypal_order_id"], row["capture_id"],
                            _time(row["hold_expires_at"]), row["error_code"])

    def get_checkout(self, checkout_id: str) -> CheckoutView:
        with self._tx() as c:
            return self._view(c, self._row(c, checkout_id))

    def list_checkouts(self) -> list[CheckoutView]:
        with self._tx() as c:
            rows = c.execute("SELECT * FROM checkouts ORDER BY created_at DESC").fetchall()
            return [self._view(c, row) for row in rows]

    def events(self, limit: int = 100) -> list[dict]:
        with self._tx() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM (SELECT * FROM events ORDER BY id DESC LIMIT ?) ORDER BY id", (limit,))]

    def ai_usage(self) -> dict:
        with self._tx() as c:
            row = c.execute("SELECT MAX(model) AS model, COUNT(DISTINCT conversation_id) AS conversations,"
                            " COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS input_tokens,"
                            " COALESCE(SUM(output_tokens),0) AS output_tokens,"
                            " COALESCE(SUM(cost_micro_usd),0) AS cost_micro_usd FROM model_usage").fetchone()
            return dict(row)
