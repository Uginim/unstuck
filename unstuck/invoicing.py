"""PayPal Invoicing v2 over the shared PayPalClient transport (token refresh, full-content logging)."""
import uuid
from copy import deepcopy

from daybreak.paypal import PayPalClient, cents_to_value, value_to_cents


class InvoicingError(RuntimeError):
    def __init__(self, what, response):
        super().__init__(f"{what} failed: HTTP {response.status_code} {response.text[:300]}")
        self.status = response.status_code
        self.debug_id = response.headers.get("Paypal-Debug-Id")


def summarize(inv: dict) -> dict:
    """The facts the agent and the UI need, in cents. Only PayPal's own fields, nothing inferred."""
    amount = inv.get("amount", {})
    due = inv.get("due_amount") or {}
    paid = (inv.get("payments") or {}).get("paid_amount") or {}
    detail = inv.get("detail", {})
    cfg = inv.get("configuration", {})
    recips = inv.get("primary_recipients") or [{}]
    return {
        "id": inv.get("id"),
        "number": detail.get("invoice_number"),
        "status": inv.get("status"),
        "currency": amount.get("currency_code"),
        "total_cents": value_to_cents(amount["value"]) if amount.get("value") else None,
        "paid_cents": value_to_cents(paid["value"]) if paid.get("value") else 0,
        "due_cents": value_to_cents(due["value"]) if due.get("value") else None,
        "invoice_date": detail.get("invoice_date"),
        "due_date": (detail.get("payment_term") or {}).get("due_date"),
        "recipient": ((recips[0].get("billing_info") or {}).get("email_address")),
        "allow_partial_payment": bool(cfg.get("partial_payment", {}).get("allow_partial_payment")),
        "minimum_due_cents": value_to_cents(cfg["partial_payment"]["minimum_amount_due"]["value"])
        if cfg.get("partial_payment", {}).get("minimum_amount_due") else None,
        "payments": [
            {"method": t.get("method"), "date": t.get("payment_date"), "note": t.get("note"),
             "cents": value_to_cents(t["amount"]["value"]) if t.get("amount") else None}
            for t in (inv.get("payments") or {}).get("transactions", [])
        ],
        "reminders_sent": (sum("REMIND" in str(h) for h in detail["metadata"]["history"])
                           if isinstance((detail.get("metadata") or {}).get("history"), list) else None),
        "items": [{"name": i.get("name"), "quantity": i.get("quantity"),
                   "unit_cents": value_to_cents(i["unit_amount"]["value"])} for i in inv.get("items", [])],
        "payer_view_url": (detail.get("metadata") or {}).get("recipient_view_url"),
    }


class Invoicing:
    def __init__(self, client: PayPalClient):
        self.pp = client

    async def _ok(self, what, method, path, json=None, ok=(200, 201, 202, 204), request_id=None):
        headers = {"Content-Type": "application/json", "Prefer": "return=representation"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        r = await self.pp._send(method, path, headers=headers, json=json)
        if r.status_code not in ok:
            raise InvoicingError(what, r)
        return r.json() if r.content else {}

    async def get(self, invoice_id: str) -> dict:
        return await self._ok("get invoice", "GET", f"/v2/invoicing/invoices/{invoice_id}")

    async def search_recipient(self, email: str) -> list[dict]:
        """Search every page, then GET complete invoices and enforce exact recipient matching."""
        if not isinstance(email, str) or not email.strip():
            raise ValueError("recipient email is required")
        invoices, seen = [], set()
        for page in range(1, 101):
            result = await self._ok("search invoices", "POST",
                f"/v2/invoicing/search-invoices?page={page}&page_size=100&total_required=true",
                {"recipient_email": email})
            rows = result.get("items", [] if result.get("total_items") == 0 else None)
            if not isinstance(rows, list):
                raise RuntimeError("Incomplete PayPal search response; cannot rule out duplicates")
            for row in rows:
                invoice_id = row["id"]
                if invoice_id in seen:
                    raise RuntimeError("Repeated PayPal search result; cannot rule out duplicates")
                seen.add(invoice_id)
                invoice = await self.get(invoice_id)
                recipients = [r.get("billing_info", {}).get("email_address", "").casefold()
                              for r in invoice.get("primary_recipients", [])]
                if email.casefold() in recipients:
                    if not isinstance(invoice.get("items"), list) or not invoice["items"]:
                        raise RuntimeError("Missing invoice items; cannot rule out duplicates")
                    invoices.append(invoice)
            pages = result.get("total_pages")
            if (pages is not None and page >= int(pages)) or (pages is None and len(rows) < 100):
                return invoices
            if not rows:
                raise RuntimeError("Missing PayPal search page; cannot rule out duplicates")
        raise RuntimeError("Too many search pages; cannot safely rule out duplicates")

    async def create_and_send(self, *, number, recipient_email, recipient_name, items, note, terms_days=15,
                              invoice_date=None, currency="USD", request_id=None, references=None) -> dict:
        request_id = request_id or str(uuid.uuid4())
        given, _, surname = recipient_name.partition(" ")
        body = {
            "detail": {"currency_code": currency, "invoice_number": number, "note": note,
                       "payment_term": {"term_type": f"NET_{terms_days}"}},
            "primary_recipients": [{"billing_info": {"name": {"given_name": given, "surname": surname or given},
                                                     "email_address": recipient_email}}],
            "items": [{"name": n, "quantity": "1", "unit_amount": {"currency_code": currency, "value": cents_to_value(c)}}
                      for n, c in items],
        }
        if references is not None:
            if len(references) != len(items):
                raise ValueError("One agreement reference is required per item")
            for item, reference in zip(body["items"], references):
                item["description"] = reference
        if invoice_date:
            body["detail"]["invoice_date"] = invoice_date
        created = await self._ok("create invoice", "POST", "/v2/invoicing/invoices", body, request_id=request_id)
        inv_id = created.get("id") or created["href"].rsplit("/", 1)[-1]
        await self._ok("send invoice", "POST", f"/v2/invoicing/invoices/{inv_id}/send", {"send_to_invoicer": False},
                       request_id=str(uuid.uuid5(uuid.NAMESPACE_URL, request_id + ":send")))
        result = await self.get(inv_id)
        if references is not None and [i.get("description") for i in result.get("items", [])] != references:
            raise RuntimeError("Agreement references not preserved; review invoice before retry")
        return result

    async def remind(self, invoice_id: str, subject: str, note: str) -> None:
        await self._ok("send reminder", "POST", f"/v2/invoicing/invoices/{invoice_id}/remind",
                       {"subject": subject, "note": note, "send_to_invoicer": False})

    async def allow_partial(self, invoice_id: str, minimum_cents: int) -> dict:
        """Invoice update is a full replace: read, change only partial_payment, write back, verify."""
        inv = await self.get(invoice_id)
        before = summarize(inv)
        if (type(minimum_cents) is not int or not 0 < minimum_cents < (before["due_cents"] or 0)
                or before["status"] in ("PAID", "MARKED_AS_PAID", "CANCELLED", "REFUNDED")):
            raise ValueError("minimum must be positive and below the current amount due on an unpaid invoice")
        body = deepcopy({k: v for k, v in inv.items()
                         if k not in ("links", "payments", "refunds", "due_amount", "gratuity")})
        body.setdefault("configuration", {})["partial_payment"] = {
            "allow_partial_payment": True,
            "minimum_amount_due": {"currency_code": before["currency"], "value": cents_to_value(minimum_cents)}}
        await self._ok("update invoice", "PUT", f"/v2/invoicing/invoices/{invoice_id}?send_to_recipient=true&send_to_invoicer=false", body)
        updated = await self.get(invoice_id)
        for key in ("items", "amount", "primary_recipients"):
            if inv.get(key) != updated.get(key):
                raise RuntimeError(f"invoice changed unexpectedly: {key}")
        after = summarize(updated)
        for key in ("total_cents", "items", "recipient", "currency", "paid_cents"):
            if before[key] != after[key]:
                raise RuntimeError(f"invoice changed unexpectedly: {key} {before[key]} -> {after[key]}")
        minimum = updated.get("configuration", {}).get("partial_payment", {}).get("minimum_amount_due", {})
        if (not after["allow_partial_payment"] or after["minimum_due_cents"] != minimum_cents
                or minimum.get("currency_code") != before["currency"]):
            raise RuntimeError("partial payment setting not applied")
        return after

    async def record_external_payment(self, invoice_id: str, cents: int, date: str, note: str, currency="USD") -> None:
        """Seed only: marks a bank transfer the client really made in the demo story. The agent cannot call this."""
        await self._ok("record payment", "POST", f"/v2/invoicing/invoices/{invoice_id}/payments",
                       {"method": "BANK_TRANSFER", "payment_date": date, "note": note,
                        "amount": {"currency_code": currency, "value": cents_to_value(cents)}})

    async def cancel(self, invoice_id: str) -> None:
        """Demo reset only."""
        await self._ok("cancel invoice", "POST", f"/v2/invoicing/invoices/{invoice_id}/cancel",
                       {"send_to_recipient": False, "send_to_invoicer": False})
