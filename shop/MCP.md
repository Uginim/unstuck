# Buyer MCP interface

Offline launch: `uv run python -m shop.mcp --dummy` from the repository root. It uses a temporary SQLite database and HTTP Dummy PayPal, ignores provider environment variables, and never calls a model or PayPal. The fixture always interprets an inquiry as 12 peony bouquets; it is not a language-understanding demo. Its approval link is a dummy URL, not a usable Sandbox checkout. Restart discards the fixture.

MCP client configuration (use the absolute path of your checkout):

```json
{
  "mcpServers": {
    "shop-dummy": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/checkout", "run", "python", "-m", "shop.mcp", "--dummy"]
    }
  }
}
```

The adapter implements newline-delimited stdio JSON-RPC, initialization, ping, tools/list and tools/call for MCP 2025-03-26. Reference: [transport](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports), [tools](https://modelcontextprotocol.io/specification/2025-03-26/server/tools). No remote HTTP endpoint or new dependency.

| Tool | Inputs | Effect |
| --- | --- | --- |
| inquire | message, optional conversation_id | Conversation and authoritative offer cards |
| select_offer | offer_id | Reserve stock, create checkout; buyer consent required |
| payment_link | checkout_id | Retrieve approval URL, retry unresolved order creation with the existing key |
| status | checkout_id | Read local status without triggering capture or claiming fresh provider verification |

All results carry `payment_mode` and USD currency. Only identifiers issued by this adapter instance are accepted. Other buyers' conversations, offers and checkouts are rejected even if their IDs are known. This is process-scoped ownership, not account authentication. Do not share an instance among buyers. Reconnection starts a new scope. The transport caps line length at 65,536 characters, each message at 4,000 characters, tool attempts after argument validation at 200 per instance. Model prose remains untrusted; code-generated offer lines and totals are authoritative.

Application integration: await `shop.mcp.serve(service, payment_mode="dummy")` with an already constructed `ShopService`. Use `sandbox` only when the injected provider really is Sandbox. The application factory owns provider lifecycle, actual buyer approval/return routing, and capture/reconciliation. No capture or seller-admin tool is exposed. `shop/main.py` belongs to the agent worker; integration request is recorded in CONTRACT-CHANGES.md.

Independent attacks: `uv run pytest tests/test_shop_attacks.py -q`. Synthetic cases are in `tests/fixtures/shop_attacks.json`. They inject hostile model outputs deliberately: they do not measure real-model susceptibility or real-user/business impact. Service failures remain ordinary failing tests, not xfails. See the coordination report for baseline failures. Payment re-query expectation comes from plan invariant 5; this is a contract gap, not evidence that the provider's write response is false.
