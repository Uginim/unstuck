"""PayPal REST adapter, written from the Payments v2 OpenAPI spec.

Error mapping is provisional until observed against the real Sandbox. The rule it follows:
a result is failed_definite only when we are confident no refund exists for this attempt.
Anything else is unknown, which keeps the reservation and retries only with the same key.
"""
import time
from decimal import Decimal

import httpx

from .logbook import Logbook
from .ports import CaptureFacts, OrderResult, RefundResult

SANDBOX_URL = "https://api-m.sandbox.paypal.com"
TIMEOUT_SECONDS = 20.0

# The money may already be out, so these must never release a reservation.
ALREADY_REFUNDED_ISSUES = {"CAPTURE_FULLY_REFUNDED", "DUPLICATE_REFUND", "REFUND_AMOUNT_EXCEEDED"}
IN_PROGRESS_ISSUES = {"PREVIOUS_REQUEST_IN_PROGRESS"}
STATUS_MAP = {
    "COMPLETED": "completed",
    "PENDING": "pending",
    "FAILED": "failed_definite",
    "CANCELLED": "failed_definite",
}


def cents_to_value(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def value_to_cents(value: str) -> int:
    return int(Decimal(value) * 100)


def _issue(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"http_{response.status_code}"
    details = body.get("details") or []
    if details and details[0].get("issue"):
        return details[0]["issue"]
    return body.get("name") or f"http_{response.status_code}"


def _from_refund_body(body: dict) -> RefundResult:
    status = STATUS_MAP.get(body.get("status", ""), "unknown")
    return RefundResult(status, refund_id=body.get("id"), error_code=None if status != "unknown" else "unexpected_status")


def _from_order_body(body: dict) -> OrderResult:
    """COMPLETED on an order alone is not money: only a completed capture counts as paid."""
    captures = [c for unit in body.get("purchase_units", []) for c in unit.get("payments", {}).get("captures", [])]
    approve = next((l["href"] for l in body.get("links", []) if l.get("rel") in ("payer-action", "approve")), None)
    if captures:
        capture = captures[0]
        status = {"COMPLETED": "paid", "PENDING": "pending", "DECLINED": "failed_definite", "FAILED": "failed_definite"}
        return OrderResult(
            status.get(capture.get("status"), "unknown"), order_id=body["id"], capture_id=capture.get("id"),
            amount_cents=value_to_cents(capture["amount"]["value"]) if capture.get("amount") else None,
        )
    state = {"CREATED": "created", "PAYER_ACTION_REQUIRED": "created", "APPROVED": "approved",
             "SAVED": "created", "VOIDED": "failed_definite"}
    return OrderResult(state.get(body.get("status"), "unknown"), order_id=body["id"], approve_url=approve)


class PayPalClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = SANDBOX_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        log: Logbook | None = None,
    ):
        self._auth = (client_id, client_secret)
        self._http = httpx.AsyncClient(base_url=base_url, timeout=TIMEOUT_SECONDS, transport=transport)
        self._token: str | None = None
        self._token_expires = 0.0
        self._log = log or Logbook(None)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _bearer(self, *, force: bool = False) -> str:
        if force or not self._token or time.monotonic() >= self._token_expires:
            response = await self._http.post(
                "/v1/oauth2/token", data={"grant_type": "client_credentials"}, auth=self._auth
            )
            response.raise_for_status()
            body = response.json()
            self._token = body["access_token"]
            self._token_expires = time.monotonic() + max(int(body.get("expires_in", 0)) - 60, 0)
        return self._token

    async def _send(self, method: str, path: str, *, headers=None, json=None) -> httpx.Response:
        started = time.monotonic()
        token = await self._bearer()
        request_headers = {"Authorization": f"Bearer {token}", **(headers or {})}
        response = await self._http.request(method, path, headers=request_headers, json=json)
        if response.status_code == 401:
            token = await self._bearer(force=True)
            request_headers["Authorization"] = f"Bearer {token}"
            response = await self._http.request(method, path, headers=request_headers, json=json)
        self._log.write(
            "paypal_call",
            method=method,
            path=path,
            request_id=(headers or {}).get("PayPal-Request-Id"),
            request_body=json,
            status=response.status_code,
            response_body=response.text,
            debug_id=response.headers.get("Paypal-Debug-Id"),
            ms=round((time.monotonic() - started) * 1000),
        )
        return response

    async def get_capture(self, capture_id: str) -> CaptureFacts | None:
        try:
            response = await self._send("GET", f"/v2/payments/captures/{capture_id}")
            if response.status_code != 200:
                return None
            body = response.json()
            return CaptureFacts(
                capture_id=body["id"],
                status=body["status"],
                amount_cents=value_to_cents(body["amount"]["value"]),
                currency=body["amount"]["currency_code"],
            )
        except (httpx.HTTPError, KeyError, ValueError) as error:
            self._log.write("paypal_error", path="capture", error=repr(error))
            return None

    async def refund(
        self, capture_id: str, amount_cents: int, currency: str, request_id: str
    ) -> RefundResult:
        try:
            await self._bearer()
        except httpx.HTTPError as error:
            # The refund request was never sent.
            self._log.write("paypal_error", path="token", error=repr(error))
            return RefundResult("failed_definite", error_code="token_unavailable")

        try:
            response = await self._send(
                "POST",
                f"/v2/payments/captures/{capture_id}/refund",
                headers={"PayPal-Request-Id": request_id, "Prefer": "return=representation"},
                json={"amount": {"value": cents_to_value(amount_cents), "currency_code": currency}},
            )
        except httpx.HTTPError as error:
            self._log.write("paypal_error", path="refund", request_id=request_id, error=repr(error))
            return RefundResult("unknown", error_code=type(error).__name__)

        if response.status_code in (200, 201):
            try:
                return _from_refund_body(response.json())
            except ValueError:
                return RefundResult("unknown", error_code="unreadable_response")

        issue = _issue(response)
        if issue in ALREADY_REFUNDED_ISSUES or issue in IN_PROGRESS_ISSUES:
            return RefundResult("unknown", error_code=issue)
        if response.status_code in (400, 401, 403, 404, 422):
            return RefundResult("failed_definite", error_code=issue)
        return RefundResult("unknown", error_code=issue)  # 409, 429, 5xx

    async def create_order(
        self, amount_cents: int, currency: str, request_id: str, reference: str, return_url: str, cancel_url: str
    ) -> OrderResult:
        return await self._order_call("POST", "/v2/checkout/orders", request_id, {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": reference,
                "amount": {"value": cents_to_value(amount_cents), "currency_code": currency},
            }],
            "payment_source": {"paypal": {"experience_context": {
                "return_url": return_url, "cancel_url": cancel_url, "user_action": "PAY_NOW",
            }}},
        })

    async def capture_order(self, order_id: str, request_id: str) -> OrderResult:
        result = await self._order_call("POST", f"/v2/checkout/orders/{order_id}/capture", request_id, {})
        if result.error_code == "ORDER_ALREADY_CAPTURED":  # Our earlier call worked; read the fact.
            return await self.get_order(order_id)
        return result

    async def get_order(self, order_id: str) -> OrderResult:
        try:
            response = await self._send("GET", f"/v2/checkout/orders/{order_id}")
        except httpx.HTTPError as error:
            self._log.write("paypal_error", path="order_get", error=repr(error))
            return OrderResult("unknown", order_id=order_id, error_code=type(error).__name__)
        return self._order_result(response, order_id)

    async def _order_call(self, method: str, path: str, request_id: str, body: dict) -> OrderResult:
        try:
            await self._bearer()
        except httpx.HTTPError as error:
            self._log.write("paypal_error", path="token", error=repr(error))
            return OrderResult("failed_definite", error_code="token_unavailable")
        try:
            response = await self._send(
                method, path, json=body,
                headers={"PayPal-Request-Id": request_id, "Prefer": "return=representation"},
            )
        except httpx.HTTPError as error:
            self._log.write("paypal_error", path=path, request_id=request_id, error=repr(error))
            return OrderResult("unknown", error_code=type(error).__name__)
        return self._order_result(response, None)

    @staticmethod
    def _order_result(response: httpx.Response, order_id: str | None) -> OrderResult:
        if response.status_code in (200, 201):
            try:
                return _from_order_body(response.json())
            except (ValueError, KeyError, IndexError, TypeError):
                return OrderResult("unknown", order_id=order_id, error_code="unreadable_response")
        issue = _issue(response)
        if response.status_code in (400, 401, 403, 404, 422) and issue != "ORDER_ALREADY_CAPTURED":
            return OrderResult("failed_definite", order_id=order_id, error_code=issue)
        return OrderResult("unknown", order_id=order_id, error_code=issue)

    async def get_refund(self, refund_id: str) -> RefundResult:
        try:
            response = await self._send("GET", f"/v2/payments/refunds/{refund_id}")
            if response.status_code != 200:
                return RefundResult("unknown", refund_id=refund_id, error_code=_issue(response))
            return _from_refund_body(response.json())
        except (httpx.HTTPError, ValueError) as error:
            self._log.write("paypal_error", path="refund_get", error=repr(error))
            return RefundResult("unknown", refund_id=refund_id, error_code=type(error).__name__)
