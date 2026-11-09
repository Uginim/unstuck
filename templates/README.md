# Connected screens and static previews

`daybreak.web.router` is included by `daybreak.main.create_app`. It uses only `RefundService` for application state; the view layer contains no SQL or refund eligibility decisions.

Routes: `/login`, `/seller`, `/seller/cases/{request_id}`, `/order/{signed_token}`. Customer links are issued by `auth.make_order_token`; possession grants access, not verified identity. Seller POSTs require the authenticated cookie and a CSRF form token. HTML responses suppress caching and referrers, escape customer text, and include a payment-mode banner even for handled errors. Dummy screens always say **Dummy PayPal, not Sandbox**.

Delegation expiry accepts an ISO timestamp with an explicit timezone, for example `2026-11-10T08:00:00Z`. The dashboard reads the latest delegation and pause status from `RefundService`; stop uses the returned ID, with no browser-cookie copy of delegation state. Resume requires an explicit reconciliation acknowledgment and is rejected by the service while unknown jobs remain. Unknown detail pages explain that a retry outside the service window stays unknown; the current view contract does not expose that specific error code.

The existing `operator.html` and `customer.html` remain standalone synthetic layout examples, distinct from the connected templates. Their `preview.js` never sends payment or model requests. Connected pages do not load this script.

Responsive CSS, native forms/disclosures, focus indicators and labels are included. HTTP tests cover customer confirmation, seller login, approval, delegation start/stop, reconciliation, stale versions, authorization and escaping. Real browser phone-width rendering and keyboard navigation have not been verified; do not treat HTTP checks as visual acceptance.
