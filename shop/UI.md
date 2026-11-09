# Shop UI

`shop.web.router` presents buyer conversations, service-built offers, checkout return,
and an authenticated seller desk. It does not calculate offers, mutate inventory,
or write the database. The fictional shop is labelled as such.

## App integration

The app supplies `state.service` (ShopService), `state.paypal_mode` (`dummy` or
`sandbox`), and `state.agent_mode` (`dummy` or `live`). For the simulated buyer
approval button only, supply `state.shop_dummy` with the DummyPayPal instance.
The button and endpoint are disabled outside dummy payment mode.

Use `/paid` and `/cancel` as the PayPal return and cancellation URLs. The return
requires the signed browser session and a matching provider order token. A GET
never captures. The buyer explicitly completes checkout with a CSRF-protected POST.
All sandbox approval links must use `https://www.sandbox.paypal.com`.

Authentication reuses `daybreak.auth`: configure `DAYBREAK_SECRET` (at least
32 characters) and `DAYBREAK_SELLER_PASSWORD` through the existing deployment
configuration. Never commit credentials. Buyer sessions use signed, HttpOnly,
SameSite=Lax cookies so they survive an external checkout return. Seller cookies
use SameSite=Strict. Cookie security follows the URL scheme/host.

Static assets are available from the router at `/static/shop/shop.css` and
`/static/shop/timeline.js`; the main app may instead mount `shop/static` at that
prefix. No inline scripts are required. Timeline events remain visible without JS.

## Verification (2026-11-09)

All transaction results here are **dummy results**, not Sandbox results.

- `uv run pytest -q`: 152 passed, 19 subtests passed on this UI branch based on c415e10.
- 13 UI tests cover offers, reservations, payment return, duplicate completion,
  lost-response reconciliation, buyer isolation, CSRF, input limits, HTML escaping,
  seller authentication, expiry, cost visibility and approval URL restrictions.
- A real local uvicorn process served the scripted-agent/DummyPayPal workflow over
  HTTP: 12 peony request, 8 peony + 4 tulip offer, reservation, simulated approval,
  completion, seller stock and timeline. No external API calls.
- Agent branch commit 6147ea1's `shop.main` and `shop.agent` were composed with this
  UI in memory, without merging or modifying that branch. Its Korean dummy request
  completed checkout; its static mount served the stylesheet.
- `node --check shop/static/timeline.js` passed. Replay was not exercised in a browser.
- Browser creation and browser inventory failed with “browser access host is
  unavailable”. Actual phone rendering and keyboard traversal remain unverified.
  CSS includes a single-column mobile layout, wrapping IDs, labelled inputs,
  visible focus and a skip link; these are implementation checks, not visual QA.

The initial UI run had one test failure: an empty request had no form content type
and correctly returned 422 rather than the expected CSRF 403. The test now sends an
invalid CSRF field as form data and checks 403. No core expectations were changed.

## Known limits

The separately reported core attack suite has seven unresolved failures at this
baseline: five misleading money replies, a boolean quantity accepted as one, and
no independent provider read after ordinary capture. Those tests are not merged
into this UI branch; the passing count above does not resolve them. The UI uses
service checkout states and does not claim it independently verifies payment.

Delivery dates are requests only: there is no delivery-date field in the service
contract. Refreshing the shop page shows the catalog; the next message retrieves
conversation history through `message`. Checkout recovery requires the same valid
browser cookie. Existing in-progress checkout selection cannot be replaced with
another offer through this browser flow.

AI cost amounts are hidden for dummy payment or unverified/dummy model mode.
In live model + Sandbox mode, displayed amounts are successful calls' recorded
API usage priced at standard rates, not actual bills. Failed calls can be absent,
and credits/free tiers are not reflected. The service's single model label does
not enumerate every model used. Cost details are seller-only.
