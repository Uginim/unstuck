# Shop conversation providers

`shop.agent` implements `SalesAgent.interpret` and `compose`. Interpretation returns a
catalog ID and quantity; `ShopService` alone builds offers and changes inventory.
Composition receives only those offers and may suggest one ID. Conversation history is
JSON data, never a system message. Prompts are public in `shop/agent.py`.

## Configuration

`SHOP_AGENT_PROVIDER` is `dummy` (default), `anthropic`, `openai`, or `gemini`.
The three provider defaults are `claude-haiku-5-5`, `gpt-5-nano`, and
`gemini-2.5-flash-lite`. `SHOP_MODEL` selects a model in the single `PRICES` table
belonging to the selected provider. Unknown prices/models are refused, not guessed.
Real providers require their corresponding `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
or `GEMINI_API_KEY`. Missing credentials for an explicitly selected provider stop
startup; they do not silently turn a real-mode run into a dummy run.

No model quality comparison has been run. The dummy is the default, not a winning
model selection. OpenAI's model page marked GPT-5 nano deprecated when checked on
2026-11-09; availability must be verified before selecting it for a live demo.

The dummy recognizes a limited one-product/numeric-quantity grammar, including
`12 peony bouquets for tomorrow` and `내일 피오니 꽃다발 12개`. Unknown, negated or
compound requests require clarification. Dummy replies identify themselves and
record no model calls. Dates are not fulfillment commitments: the service has no
delivery-calendar contract.

## App integration

After combining the independently owned `shop.web` and `shop/static` files:

```sh
SHOP_DUMMY_PAYPAL=1 SHOP_AGENT_PROVIDER=dummy uv run uvicorn shop.main:create_app --factory
```

`SHOP_DB` defaults to `shop.db`, `SHOP_LOG` to `logs/shop.jsonl`, and
`SHOP_BASE_URL` to `http://localhost:8000`. Return and cancel URLs are `/paid` and
`/cancel`. `DAYBREAK_DUMMY_PAYPAL=1` is accepted when `SHOP_DUMMY_PAYPAL` is unset.
The dummy payment flag wins over credentials. Without that flag, both PayPal
credential variables are required and the existing Sandbox-only client is used.
No `.env` file is loaded. Only the dummy payment mode seeds the fictional catalog.
A Sandbox database must have its catalog provisioned separately.

The app exposes `state.service`, `state.paypal_mode` (`dummy` or `sandbox`),
`state.agent_mode` (`dummy` or `live`), `state.agent_provider`, and
`state.shop_dummy` (instance or `None`). Static files mount at `/static/shop`.
The UI must identify payment and model modes separately. The factory test uses a
stub UI router; it does not verify the separately developed UI.

## Usage and cost boundaries

`PRICES` contains direct-API standard **paid-tier list prices**, checked 2026-11-09:
[Anthropic](https://platform.claude.com/docs/en/about-claude/pricing),
[OpenAI](https://developers.openai.com/api/docs/models/gpt-5-nano),
[Gemini](https://ai.google.dev/gemini-api/docs/pricing).

Only API-returned token counts are used. Cached input is accounted for separately;
Gemini thinking tokens are included in output and OpenAI output counts already
include reasoning. Haiku's over-100K input tier is accounted for. Costs are rounded
to the nearest micro-USD per call. No tools, explicit caching, batching or retries
are requested. Unexpected Anthropic cache-creation usage is rejected rather than
priced with a guessed cache lifetime.

This is a **usage-derived list-price cost**, not an actual billed charge. Free-tier
allowances, credits, taxes and account discounts are not inferable from token
usage. In particular, Gemini free-tier calls must not be presented as paid charges.
Missing/invalid usage raises an exception; it is never replaced with fabricated
zero usage. Response truncation, refusal, HTTP errors and parser failures also
raise and are handled by the existing service fallback.

Successful usage parsing is logged as `sales_model_usage` before output parsing.
The current protected service only inserts usage from successfully returned turns,
so its aggregate excludes calls whose output is subsequently rejected and calls
that fail without usage. It is not a complete account bill. This contract limit
has been reported to the service owner. A 0-call aggregate means **no recorded
successful calls**, not proof that the provider incurred no cost.

## Verification

`uv run pytest -q` covers all three HTTP request/response shapes with MockTransport,
strict product/quantity/offer validation, injection payload separation, failure
propagation, token-cost arithmetic, explicit provider selection, bilingual dummy
requests and a dummy core checkout through the app factory. No real credentials,
model requests or PayPal Sandbox transactions were used. These tests do not prove
real model quality, prompt-injection resistance or provider model availability.

Money and fulfillment claims in natural-language output remain subject to the
service's guard; a prompt is not a security boundary. Protected service behavior
was not changed by this branch.
