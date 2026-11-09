"""Shop app factory. Explicit dummy providers; no live-mode PayPal endpoint."""
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from daybreak.dummy_paypal import DummyPayPal
from daybreak.logbook import Logbook
from daybreak.main import SystemClock
from daybreak.paypal import PayPalClient
from .agent import build_agent
from .seed import seed
from .service import ShopService


def create_app():
    from .web import router

    log = Logbook(os.environ.get('SHOP_LOG', 'logs/shop.jsonl'))
    agent = build_agent(log)
    dummy = None
    if os.environ.get('SHOP_DUMMY_PAYPAL', os.environ.get('DAYBREAK_DUMMY_PAYPAL')) == '1':
        dummy = DummyPayPal()
        paypal = PayPalClient('dummy', 'dummy', transport=dummy.transport(), log=log)
        mode = 'dummy'
    else:
        client_id, secret = os.environ.get('PAYPAL_CLIENT_ID'), os.environ.get('PAYPAL_CLIENT_SECRET')
        if not client_id or not secret:
            raise RuntimeError('Set SHOP_DUMMY_PAYPAL=1 or sandbox PayPal credentials')
        paypal = PayPalClient(client_id, secret, log=log)
        mode = 'sandbox'
    db = os.environ.get('SHOP_DB', 'shop.db')
    base = os.environ.get('SHOP_BASE_URL', 'http://localhost:8000').rstrip('/')
    service = ShopService(db, paypal, agent, SystemClock(), log,
                          return_url=base + '/paid', cancel_url=base + '/cancel')
    if mode == 'dummy':
        seed(db)
    service.recover_on_start()

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await paypal.aclose()

    app = FastAPI(title='Shop sales assistant', lifespan=lifespan)
    app.state.service = service
    app.state.paypal_mode = mode
    app.state.agent_provider = os.environ.get('SHOP_AGENT_PROVIDER', 'dummy')
    app.state.agent_mode = 'dummy' if app.state.agent_provider == 'dummy' else 'live'
    app.state.shop_dummy = dummy

    @app.get('/health')
    def health():
        return {'status': 'ok', 'paypal_mode': mode, 'agent_mode': app.state.agent_mode}

    app.mount('/static/shop', StaticFiles(directory=Path(__file__).with_name('static'), check_dir=False), name='shop_static')
    app.include_router(router)
    return app
