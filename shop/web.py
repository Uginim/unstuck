"""Shop HTML adapter. ShopService owns offers, holds, payment decisions and persistence."""
import base64
import hmac
import json
import secrets
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates

from daybreak.auth import (SELLER_COOKIE, SELLER_SESSION_SECONDS, check_seller_password,
                           make_order_token, make_seller_cookie, require_order, require_seller)
from .service import InvalidState, NotFound, OutOfStock, ShopService

ROOT = Path(__file__).parent
COOKIE = 'shop_buyer'
views = Jinja2Templates(directory=ROOT / 'templates')
views.env.filters['usd'] = lambda cents: f'USD {Decimal(cents) / 100:,.2f}'
views.env.filters['cost'] = lambda micro, count=1: f'USD {Decimal(micro) / 1_000_000 / count:.6f}'
LABELS = {'held': 'Preparing checkout', 'awaiting_payment': 'Awaiting buyer approval',
          'capturing': 'Payment processing', 'unknown': 'Payment not yet confirmed',
          'paid': 'Payment confirmed', 'failed': 'Checkout failed', 'expired': 'Reservation expired',
          'order_created': 'PayPal checkout created', 'amount_mismatch': 'Payment needs seller review'}
views.env.filters['state_label'] = lambda state: LABELS.get(state, 'Recorded update')
views.env.filters['offer_label'] = lambda kind: {
    'exact': 'Your requested flowers', 'substitute': 'Seller-approved alternative',
    'partial': 'Available quantity only'}.get(kind, 'Available selection')


def service(request: Request) -> ShopService:
    return request.app.state.service


def secure(request):
    return request.url.scheme == 'https' or request.url.hostname not in {'localhost', '127.0.0.1', '::1', 'testserver'}


def session(request):
    if hasattr(request.state, 'shop_session'):
        return request.state.shop_session
    value = None
    token = request.cookies.get(COOKIE)
    if token:
        try:
            subject = require_order(token)
            if subject.startswith('shop:'):
                value = json.loads(base64.urlsafe_b64decode(subject[5:]))
        except (HTTPException, ValueError, TypeError):
            pass
    if not isinstance(value, dict):
        value = {'csrf': secrets.token_urlsafe(32)}
    request.state.shop_session = value
    return value


def finish(request, response):
    encoded = base64.urlsafe_b64encode(json.dumps(session(request)).encode()).decode()
    response.set_cookie(COOKIE, make_order_token('shop:' + encoded, int(time.time()) + 43200),
                        max_age=43200, httponly=True, samesite='lax', secure=secure(request))
    response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                             'X-Content-Type-Options': 'nosniff',
                             'Content-Security-Policy': "default-src 'self'; style-src 'self'; script-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})
    return response


def render(request, page, status=200, **context):
    try:
        require_seller(request.cookies.get(SELLER_COOKIE))
        signed_in = True
    except HTTPException:
        signed_in = False
    return finish(request, views.TemplateResponse(request=request, name=page, context={
        'csrf': session(request)['csrf'], 'mode': getattr(request.app.state, 'paypal_mode', 'unconfigured'),
        'agent_mode': getattr(request.app.state, 'agent_mode', 'unverified'),
        'signed_in': signed_in, **context}, status_code=status))


class HTMLRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            try:
                return await handler(request)
            except (HTTPException, NotFound, InvalidState, OutOfStock) as error:
                status = error.status_code if isinstance(error, HTTPException) else (404 if isinstance(error, NotFound) else 409)
                message = {401: 'Sign in to view the seller desk.', 403: 'This form expired. Reload and try again.',
                           404: 'This checkout or selection is not available in this browser.',
                           409: 'Availability or checkout state changed. Check your checkout before requesting new offers.',
                           413: 'Your message is too long.', 422: 'Enter a message of up to 2,000 characters.'}.get(status, 'This action is unavailable.')
                return render(request, 'error.html', status=status, message=message)
        return handle


router = APIRouter(route_class=HTMLRoute)


async def form(request):
    if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
        raise HTTPException(422)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 32768:
            raise HTTPException(413)
    try:
        fields = parse_qs(body.decode('utf-8'), max_num_fields=12, keep_blank_values=True)
    except (ValueError, UnicodeError):
        raise HTTPException(422) from None
    if any(len(v) != 1 for v in fields.values()):
        raise HTTPException(422)
    data = {k: v[0] for k, v in fields.items()}
    if not hmac.compare_digest(session(request)['csrf'].encode(), data.get('csrf', '').encode()):
        raise HTTPException(403)
    return data


def current_checkout(request):
    checkout_id = session(request).get('checkout')
    if not checkout_id:
        raise HTTPException(404)
    return service(request).get_checkout(checkout_id)


def checkout_page(request, checkout, **context):
    url = urlsplit(checkout.approve_url or '')
    # Only sandbox checkout links are accepted here; the app has no production mode contract.
    approval = checkout.approve_url if (getattr(request.app.state, 'paypal_mode', '') == 'sandbox'
        and url.scheme == 'https' and url.hostname == 'www.sandbox.paypal.com' and not url.username) else None
    return render(request, 'checkout.html', checkout=checkout, approval=approval,
                  dummy_ready=getattr(request.app.state, 'shop_dummy', None) is not None, **context)


@router.get('/static/shop/shop.css')
async def css():
    return FileResponse(ROOT / 'static/shop.css', media_type='text/css')


@router.get('/static/shop/timeline.js')
async def script():
    return FileResponse(ROOT / 'static/timeline.js', media_type='text/javascript')


@router.get('/')
async def home(request: Request):
    return render(request, 'buyer.html', catalog=service(request).catalog(), conversation=None)


@router.post('/message')
async def message(request: Request):
    data = await form(request)
    text = data.get('message', '').strip()
    if not text or len(text) > 2000:
        raise HTTPException(422)
    state = session(request)
    view = await service(request).message(text, state.get('conversation'))
    state.update(conversation=view.id, offers=[offer.id for offer in view.offers])
    return render(request, 'buyer.html', catalog=service(request).catalog(), conversation=view)


@router.post('/choose')
async def choose(request: Request):
    data = await form(request)
    if data.get('offer') not in session(request).get('offers', []):
        raise HTTPException(404)
    existing = session(request).get('checkout')
    if existing:
        current = service(request).get_checkout(existing)
        if current.state in {'held', 'awaiting_payment', 'capturing', 'unknown'} and current.offer_id != data['offer']:
            raise HTTPException(409)
    checkout = await service(request).choose(data['offer'])
    session(request)['checkout'] = checkout.id
    return finish(request, RedirectResponse('/checkout', status_code=303))


@router.get('/checkout')
async def checkout(request: Request):
    return checkout_page(request, current_checkout(request))


@router.get('/paid')
async def paid_return(request: Request):
    checkout = current_checkout(request)
    if not checkout.paypal_order_id or request.query_params.get('token') != checkout.paypal_order_id:
        raise HTTPException(404)
    return checkout_page(request, checkout, returned=True)


@router.get('/cancel')
async def cancel_return(request: Request):
    return checkout_page(request, current_checkout(request), cancelled=True)


@router.post('/checkout/complete')
async def complete(request: Request):
    await form(request)
    checkout = current_checkout(request)
    checkout = await service(request).capture(checkout.id)
    return checkout_page(request, checkout)


@router.post('/checkout/refresh')
async def refresh(request: Request):
    await form(request)
    checkout = current_checkout(request)
    if checkout.state == 'held':
        checkout = await service(request).ensure_order(checkout.id)
    elif checkout.state == 'unknown':
        checkout = await service(request).reconcile(checkout.id)
    return checkout_page(request, checkout)


@router.post('/checkout/dummy-approve')
async def dummy_approve(request: Request):
    await form(request)
    dummy = getattr(request.app.state, 'shop_dummy', None)
    if getattr(request.app.state, 'paypal_mode', '') != 'dummy' or dummy is None:
        raise HTTPException(404)
    checkout = current_checkout(request)
    if checkout.state != 'awaiting_payment' or not checkout.paypal_order_id:
        raise HTTPException(409)
    dummy.approve_order(checkout.paypal_order_id)
    return finish(request, RedirectResponse('/paid?token=' + checkout.paypal_order_id, status_code=303))


@router.get('/seller/login')
async def login(request: Request):
    return render(request, 'login.html')


@router.post('/seller/login')
async def sign_in(request: Request):
    data = await form(request)
    if not check_seller_password(data.get('password', '')):
        return render(request, 'login.html', status=401, error='Password not recognized.')
    response = finish(request, RedirectResponse('/seller', status_code=303))
    response.set_cookie(SELLER_COOKIE, make_seller_cookie(), max_age=SELLER_SESSION_SECONDS,
                        httponly=True, samesite='strict', secure=secure(request))
    return response


@router.post('/seller/logout')
async def logout(request: Request):
    await form(request)
    response = finish(request, RedirectResponse('/', status_code=303))
    response.delete_cookie(SELLER_COOKIE)
    return response


@router.get('/seller', dependencies=[Depends(require_seller)])
async def seller(request: Request):
    svc = service(request)
    return render(request, 'seller.html', orders=svc.list_checkouts(), catalog=svc.catalog(),
                  events=svc.events(), usage=svc.ai_usage())


@router.post('/seller/reconcile/{checkout_id}', dependencies=[Depends(require_seller)])
async def seller_reconcile(request: Request, checkout_id: str):
    await form(request)
    await service(request).reconcile(checkout_id)
    return finish(request, RedirectResponse('/seller', status_code=303))


@router.post('/seller/expire', dependencies=[Depends(require_seller)])
async def expire(request: Request):
    await form(request)
    await service(request).expire_holds()
    return finish(request, RedirectResponse('/seller', status_code=303))
