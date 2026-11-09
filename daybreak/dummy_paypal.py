"""An in-memory PayPal at the HTTP layer, shaped by the Payments v2 OpenAPI spec.

The real PayPalClient runs against it, so parsing and error mapping are exercised.
It reflects the spec, not observed Sandbox behavior, and results from it are never
reported as Sandbox results. Modeling assumptions the spec does not guarantee: keys are
kept indefinitely, and a capture reads REFUNDED immediately even while its refund is pending.
"""
import json
import uuid
from decimal import Decimal

import httpx

from .paypal import cents_to_value


class DummyPayPal:
    def __init__(self) -> None:
        self.captures: dict[str, dict] = {}
        self.refunds: dict[str, dict] = {}
        self.by_request_id: dict[str, str] = {}
        self.refund_calls = 0
        self.money_out_calls = 0
        # Next outcomes for refund calls: "lost" (applied, response lost), "timeout" (not applied),
        # "500", "pending", or an issue name returned as HTTP 422.
        self.faults: list[str] = []

    def add_capture(self, capture_id: str, amount_cents: int, currency: str = "USD") -> None:
        self.captures[capture_id] = {
            "id": capture_id,
            "status": "COMPLETED",
            "amount": {"value": cents_to_value(amount_cents), "currency_code": currency},
        }

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "dummy", "expires_in": 32400})
        if path.startswith("/v2/payments/captures/") and path.endswith("/refund"):
            return self._refund(request, path.split("/")[4])
        if path.startswith("/v2/payments/captures/"):
            capture = self.captures.get(path.split("/")[4])
            return httpx.Response(200, json=capture) if capture else self._error(404, "RESOURCE_NOT_FOUND")
        if path.startswith("/v2/payments/refunds/"):
            refund = self.refunds.get(path.split("/")[4])
            return httpx.Response(200, json=refund) if refund else self._error(404, "RESOURCE_NOT_FOUND")
        return self._error(404, "NOT_FOUND")

    def _refund(self, request: httpx.Request, capture_id: str) -> httpx.Response:
        self.refund_calls += 1
        fault = self.faults.pop(0) if self.faults else None
        if fault == "timeout":
            raise httpx.ReadTimeout("dummy timeout", request=request)
        if fault == "500":
            return self._error(500, "INTERNAL_SERVER_ERROR")
        if fault and fault not in ("lost", "pending"):
            return self._error(422, fault)

        key = request.headers.get("PayPal-Request-Id", "")
        if key in self.by_request_id:
            return httpx.Response(200, json=self.refunds[self.by_request_id[key]])
        capture = self.captures.get(capture_id)
        if capture is None:
            return self._error(404, "RESOURCE_NOT_FOUND")
        if capture["status"] == "REFUNDED":
            return self._error(422, "CAPTURE_FULLY_REFUNDED")

        amount = json.loads(request.content)["amount"]
        if amount["currency_code"] != capture["amount"]["currency_code"]:
            return self._error(422, "REFUND_CURRENCY_MISMATCH")
        if Decimal(amount["value"]) > Decimal(capture["amount"]["value"]):
            return self._error(422, "REFUND_AMOUNT_EXCEEDED")

        self.money_out_calls += 1
        refund = {
            "id": f"DUMMY-{uuid.uuid4().hex[:12].upper()}",
            "status": "PENDING" if fault == "pending" else "COMPLETED",
            "amount": amount,
        }
        self.refunds[refund["id"]] = refund
        self.by_request_id[key] = refund["id"]
        capture["status"] = "REFUNDED"
        if fault == "lost":
            raise httpx.ReadTimeout("dummy lost response", request=request)
        return httpx.Response(201, json=refund)

    @staticmethod
    def _error(status: int, issue: str) -> httpx.Response:
        names = {404: "RESOURCE_NOT_FOUND", 422: "UNPROCESSABLE_ENTITY", 500: "INTERNAL_SERVER_ERROR"}
        return httpx.Response(status, json={
            "name": names.get(status, issue),
            "message": issue,
            "debug_id": "dummy-debug-id",
            "details": [{"issue": issue, "description": issue}],
        })
