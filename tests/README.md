# Independent evaluation

`cases.json` contains 24 synthetic operational scenarios and expected side effects. Expectations are fixed independently of `decide`; they are never generated from implementation outputs. Boundary labels in the draft remain explicit expectations, including known failures.

Run the whole suite on Python 3.13:

```sh
uv run --python 3.13 pytest --basetemp=.checks/pytest
```

Generate a result report with the tested code revision (nonzero exit on failures):

```sh
uv run --python 3.13 python tests/evaluate.py
```

`test_integration.py` executes all 24 scenarios with `Service`, SQLite and the real `PayPalClient` parsing `DummyPayPal` HTTP responses. Faults include pending, response loss and definite failure. Counts include every refund adapter invocation, not only simulated money movements. Separate database connections compete for reservations; an independent connection checks that no write lock spans a refund call. Restart tests reopen persisted state. Fixture SQL only establishes or inspects test conditions.

Scenario 17 additionally requires the six HTTP authorization variants in `test_web.py`. Scenario 23 includes multiple failure and incomplete-fact variants. Supplemental tests enforce unknown-retry retention and the distinction between missing evidence and missing automatic authority. The report separates the 24-scenario denominator from all pytest cases/subtests. Any critical failure means acceptance is not met, even if 24 catalog items later pass.

`test_dummy_spec.py` compares known statuses/error mappings and the error envelope against the [official Payments v2 schema](https://developer.paypal.com/api/payments/v2/schema.yaml). These are **dummy results, not Sandbox observations**. The dummy's unconditional capture update on pending, unlimited idempotency cache, and absence of request amount/currency validation are assumptions, not established provider behavior.

`test_extract.py` validates structured request construction and rejects malformed outputs using a mocked model response. It does not measure model quality. Independently authored adversarial language cases remain separate. `test_web.py` checks service wiring, auth, CSRF, escaping and mode disclosure without browser automation. Existing static-preview checks run with `node --test tests/preview.test.cjs`.
