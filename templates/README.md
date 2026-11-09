# Connected screens and static previews

`daybreak.web.router` is included by `daybreak.main.create_app`. It uses only `RefundService` for application state; the view layer contains no SQL or refund eligibility decisions.

Routes: `/login`, `/seller`, `/seller/cases/{request_id}`, `/order/{signed_token}`. Customer links are issued by `auth.make_order_token`; possession grants access, not verified identity. Seller POSTs require the authenticated cookie and a CSRF form token. HTML responses suppress caching and referrers, escape customer text, and include a payment-mode banner even for handled errors. Dummy screens always say **Dummy PayPal, not Sandbox**.

Delegation duration is selected as 1, 4, 8, 12 or 24 hours from now. The server calculates an aware UTC expiry; client-provided timestamps are ignored. The dashboard orders summary, requests (approval pending first), delegation, timeline, then AI cost, using the same document order on phones. It reads current delegation and pause status from the service. Resume requires an explicit reconciliation acknowledgment and is rejected while unknown jobs remain. Unknown detail pages explain the retry window; the view contract does not expose that specific error code.

The existing `operator.html` and `customer.html` remain standalone synthetic layout examples, distinct from the connected templates. Their `preview.js` never sends payment or model requests. Connected pages do not load this script.

Responsive CSS, native forms/disclosures, focus indicators and labels are included. HTTP tests cover customer confirmation, seller login, approval, delegation start/stop, reconciliation, stale versions, authorization and escaping. Real browser phone-width rendering and keyboard navigation have not been verified; do not treat HTTP checks as visual acceptance.


Conversation UI uses `converse` and `choose`. Only service-provided `ConversationView.options` generate choice forms; their integer cents and timestamps supply option values, with en/ko button copy selected from `language`. AI responses are escaped plain text labeled `AI reply`, with a notice for `reply_was_replaced`. A suggested option is visually marked but never automatically selected or submitted. Existing `/confirm` is retained for prior callers; the conversation page uses `/choose` exclusively. Customer results still exclude all seller delegation details.

The morning timeline renders the service events chronologically on the server. An external same-origin script provides optional replay/pause/show-all controls with no network calls, preserving the existing CSP. Without JavaScript the entire list remains visible. Booking moved/kept records explicitly say no refund was sent.

Phone CSS review: 320–420px uses a single-column summary, wrapping navigation/buttons and long conversation/event text; all grid children can shrink. Browser-host inventory currently fails with “The browser access host is unavailable”, so actual phone rendering and keyboard playback remain unverified. The replay logic has dependency-free Node tests: `node --test tests/timeline.test.cjs`.


The seller-only AI cost panel reads `ai_usage()`. Conversations are the average-cost denominator; micro-USD values are formatted without floating-point conversion. Zero measured turns show “No measured model calls”. Dummy mode suppresses all cost amounts even if usage exists. The API returns one reported model name; the UI does not infer a per-model breakdown.


Move options explain in English/Korean that the refund deadline remains based on the original session. Move results display only `CaseView.booking_starts_at` as the new date. Customer-owned interface copy, including AI reply labels and result headings, follows the language table; the required “Dummy PayPal, not Sandbox” disclosure stays verbatim. Timeline event labels are explanatory text rather than internal action codes.
