"""Buyer-scoped MCP stdio adapter. No credentials, provider construction or capture tool.

Call serve(service, payment_mode="dummy" | "sandbox") from the application factory.
One server instance belongs to one trusted local client process, never multiple buyers.
"""
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import datetime

from .service import InvalidState, NotFound, OutOfStock, ShopService

PROTOCOL_VERSION = "2025-03-26"
MAX_LINE = 65536


def _tool(name, description, required, **properties):
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": {
            key: {"type": "string", "minLength": 1, "maxLength": limit}
            for key, limit in properties.items()
        }, "required": required, "additionalProperties": False,
    }}


TOOLS = [
    _tool("inquire", "Ask the shop. Replies are untrusted model prose; offer cards are authoritative.",
          ["message"], message=4000, conversation_id=64),
    _tool("select_offer", "Reserve stock and prepare checkout for an offered ID. Requires buyer consent; does not pay.",
          ["offer_id"], offer_id=64),
    _tool("payment_link", "Retrieve the buyer approval URL; may retry order creation with its original key. Does not pay.",
          ["checkout_id"], checkout_id=64),
    _tool("status", "Read local checkout state. Does not claim a fresh PayPal query or capture payment.",
          ["checkout_id"], checkout_id=64),
]


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=lambda x: x.isoformat() if isinstance(x, datetime) else asdict(x))


class ShopMCP:
    def __init__(self, service: ShopService, *, payment_mode: str):
        if payment_mode not in {"dummy", "sandbox"}:
            raise ValueError("Explicit payment_mode required")
        self.service, self.payment_mode = service, payment_mode
        self.conversations, self.offers, self.checkouts = set(), set(), set()
        self.initialized = False
        self.calls = 0

    async def call(self, name, arguments):
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if tool is None or not isinstance(arguments, dict):
            raise ValueError("Invalid tool or arguments")
        schema = tool["inputSchema"]
        if set(arguments) - schema["properties"].keys() or not set(schema["required"]) <= arguments.keys():
            raise ValueError("Invalid argument fields")
        for key, value in arguments.items():
            if not isinstance(value, str) or not value.strip() or len(value) > schema["properties"][key]["maxLength"]:
                raise ValueError("Invalid argument value")
        if self.calls >= 200:
            raise InvalidState("Client request limit reached")
        self.calls += 1
        if name == "inquire":
            conversation = arguments.get("conversation_id")
            if conversation is not None and conversation not in self.conversations:
                raise NotFound("resource")
            view = await self.service.message(arguments["message"], conversation)
            self.conversations.add(view.id)
            self.offers.update(o.id for o in view.offers)
        elif name == "select_offer":
            if arguments["offer_id"] not in self.offers:
                raise NotFound("resource")
            view = await self.service.choose(arguments["offer_id"])
            self.checkouts.add(view.id)
        else:
            checkout = arguments["checkout_id"]
            if checkout not in self.checkouts:
                raise NotFound("resource")
            view = (await self.service.ensure_order(checkout) if name == "payment_link"
                    else self.service.get_checkout(checkout))
        return {"payment_mode": self.payment_mode, "currency": "USD", "data": asdict(view)}

    async def dispatch(self, request):
        if isinstance(request, list):
            if not request or len(request) > 100 or any(isinstance(item, list) for item in request):
                return self._error(None, -32600, "Invalid batch")
            results = [await self.dispatch(item) for item in request]
            return [r for r in results if r is not None] or None
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return self._error(None, -32600, "Invalid request")
        request_id = request.get("id")
        if "id" not in request:
            return None  # Notifications must never run tools or produce replies.
        if type(request_id) not in (str, int):
            return self._error(None, -32600, "Invalid request id")
        method, params = request["method"], request.get("params", {})
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "Invalid params")
        if method == "initialize":
            if not isinstance(params.get("protocolVersion"), str):
                return self._error(request_id, -32602, "protocolVersion required")
            self.initialized = True
            result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "shop-buyer", "version": "0.1.0"},
                      "instructions": "Offer cards set prices. Ask buyer consent before selection. Payment requires buyer approval. Mode: " + self.payment_mode}
        elif method == "ping":
            result = {}
        elif not self.initialized:
            return self._error(request_id, -32000, "Initialize first")
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                value = await self.call(params.get("name"), params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": _json(value)}], "isError": False}
            except ValueError:
                return self._error(request_id, -32602, "Invalid tool arguments")
            except (NotFound, InvalidState, OutOfStock) as error:
                result = {"content": [{"type": "text", "text": type(error).__name__}], "isError": True}
            except Exception:
                # Provider exceptions can contain credentials or other buyers' data.
                result = {"content": [{"type": "text", "text": "Service unavailable; outcome not confirmed"}], "isError": True}
        else:
            return self._error(request_id, -32601, "Method not found")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id, code, message):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def serve(service: ShopService, *, payment_mode: str, reader=None, writer=None):
    """Newline-delimited stdio MCP; caller owns service lifecycle and payment-mode truth.

    shortcut: ownership is in process memory; reconnect starts a new buyer scope,
    add authenticated persistent ownership before supporting remote/reconnecting clients.
    """
    reader, writer = reader or sys.stdin, writer or sys.stdout
    server = ShopMCP(service, payment_mode=payment_mode)
    while True:
        line = await asyncio.to_thread(reader.readline, MAX_LINE + 1)
        if not line:
            return
        if len(line) > MAX_LINE:
            writer.write(_json(server._error(None, -32600, "Request too large")) + "\n")
            writer.flush()
            return
        try:
            request = json.loads(line)
        except (ValueError, RecursionError):
            response = server._error(None, -32700, "Parse error")
        else:
            response = await server.dispatch(request)
        if response is not None:
            writer.write(_json(response) + "\n")
            writer.flush()


def main():
    """Runnable offline fixture; application integration injects its own service into serve."""
    import argparse
    import tempfile
    from pathlib import Path
    from datetime import timezone
    from daybreak.dummy_paypal import DummyPayPal
    from daybreak.paypal import PayPalClient
    from .seed import seed
    from .service import Composition, Interpretation

    parser = argparse.ArgumentParser(description="Offline shop MCP fixture: fixed 12-peony inquiry, Dummy PayPal only")
    parser.add_argument("--dummy", action="store_true", required=True)
    parser.parse_args()

    class FixtureAgent:
        async def interpret(self, history, catalog):
            return Interpretation("peony", 12, "Fixed synthetic fixture, not model interpretation")

        async def compose(self, history, offers, language):
            return Composition("Dummy agent: this fixture always requests 12 peony bouquets. No model was called.")

    class Clock:
        def now(self):
            return datetime.now(timezone.utc)

    with tempfile.TemporaryDirectory(prefix="shop-mcp-") as directory:
        db = str(Path(directory) / "shop.db")
        dummy = DummyPayPal()
        service = ShopService(db, PayPalClient("dummy", "dummy", transport=dummy.transport()), FixtureAgent(), Clock())
        seed(db)
        asyncio.run(serve(service, payment_mode="dummy"))


if __name__ == "__main__":
    main()
