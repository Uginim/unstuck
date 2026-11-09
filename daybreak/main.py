"""App wiring. PAYPAL_CLIENT_ID selects the real Sandbox; DAYBREAK_DUMMY_PAYPAL=1 selects the
dummy. With neither, the app refuses to start, so a dummy run is never mistaken for a real one."""
import os
from datetime import datetime, timezone

from fastapi import FastAPI

from .dummy_paypal import DummyPayPal
from .execute import Service
from .logbook import Logbook
from .paypal import PayPalClient
from .seed import ORDERS, seed


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def _paypal(log: Logbook) -> tuple[PayPalClient, str]:
    client_id = os.environ.get("PAYPAL_CLIENT_ID")
    if client_id:
        return PayPalClient(client_id, os.environ["PAYPAL_CLIENT_SECRET"], log=log), "sandbox"
    if os.environ.get("DAYBREAK_DUMMY_PAYPAL") == "1":
        dummy = DummyPayPal()
        for _, capture_id, cents, _ in ORDERS:
            dummy.add_capture(capture_id, cents)
        return PayPalClient("dummy", "dummy", transport=dummy.transport(), log=log), "dummy"
    raise RuntimeError("Set PAYPAL_CLIENT_ID, or DAYBREAK_DUMMY_PAYPAL=1 for a local dummy run")


def create_app() -> FastAPI:
    from .extract import build_extractor  # Codex
    from .web import router  # Codex

    log = Logbook(os.environ.get("DAYBREAK_LOG", "logs/requests.jsonl"))
    paypal, mode = _paypal(log)
    db_path = os.environ.get("DAYBREAK_DB", "daybreak.db")
    service = Service(db_path, paypal, build_extractor(log), SystemClock(), log)
    if mode == "dummy":
        seed(db_path)
    service.recover_on_start()

    app = FastAPI(title="Daybreak")
    app.state.service = service
    app.state.paypal_mode = mode  # the UI must show this on every screen

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "paypal_mode": mode}

    app.include_router(router)
    return app
