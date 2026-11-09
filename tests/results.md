# Dummy results / 더미 결과

Tested code revision: `256f9e47c5b2b41de9a94d98ac83bc63d6c6b62f`.

**Dummy PayPal, not Sandbox. No live PayPal or model calls.**

Operational catalog: **24/24 scenarios passed**. Variants inside a scenario are assertions, not separately counted successes.
Pytest report: 79 reported test cases, 0 failed, 0 skipped. Pure-policy subtests and supplemental HTTP/model-shape tests are not additional catalog scenarios.

Local uvicorn HTTP flows: 2/2 passed (normal app and injected response loss).

## Failures

None.

## Limits

This run tests the real service and adapter against a synthetic HTTP transport. It does not validate actual PayPal behavior, credentials, model accuracy, browser layout or deployment. Independent extraction adversarial cases are a separate evaluation. A passing scenario does not override failures in supplemental checks.

Reproduce with `uv run --python 3.13 python tests/evaluate.py`. The command exits nonzero on failures and preserves the expected results.
