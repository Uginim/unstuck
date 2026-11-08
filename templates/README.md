# Static layout preview

From the repository root, run `python3 -m http.server 8873 --bind 127.0.0.1` and visit `/templates/operator.html` or `/templates/customer.html`.

These are synthetic layout fixtures, not connected Jinja views. The customer confirmation and seller approval are explicitly labeled illustrations. `static/preview.js` performs no requests or storage. Replace it when connecting the service; never present its fixed example as model output or a payment result.

The layout includes a one-column breakpoint at 760px, native keyboard-operable disclosures and buttons, visible focus, explicit input labels, skip links and focus return. Browser rendering, phone overflow and real keyboard navigation remain **unverified**: browser inventory and tab creation both returned `The browser access host is unavailable` during this draft. Validate at 320px, 375px and desktop before claiming UI acceptance.

Available checks: `node --check static/preview.js`, `node --test tests/preview.test.cjs`. The latter checks illustration state and focus calls with a minimal fake document; it does not simulate a real browser.

Next integration uses only `RefundService` from the web layer. Authentication must resolve the order and seller before calling it. No local decision logic or database writes belong in the view layer. Service errors and authentication dependencies must be agreed before wiring routes.
