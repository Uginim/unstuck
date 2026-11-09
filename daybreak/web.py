"""HTML routes. Authentication and RefundService own all authority and state changes."""
import hmac
import secrets
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.routing import APIRoute
from fastapi.templating import Jinja2Templates

from .auth import (SELLER_COOKIE, SELLER_SESSION_SECONDS, check_seller_password,
                   make_seller_cookie, require_order, require_seller)
from .ports import InvalidInput, InvalidState, NotFound, RefundService, StaleVersion

ROOT = Path(__file__).resolve().parent.parent
views = Jinja2Templates(directory=ROOT / 'templates')
views.env.filters['usd'] = lambda cents: f'${cents // 100}.{cents % 100:02d}'


def secure_cookie(request: Request):
    return request.url.scheme == 'https' or request.url.hostname not in {'localhost', '127.0.0.1', '::1', 'testserver'}


def render(request: Request, page: str, *, status=200, **values):
    csrf = request.cookies.get('daybreak_csrf') or secrets.token_urlsafe(32)
    response = views.TemplateResponse(request=request, name=page, context={
        'mode': request.app.state.paypal_mode, 'csrf': csrf, **values,
    }, status_code=status)
    response.set_cookie('daybreak_csrf', csrf, httponly=True, samesite='strict',
                        secure=secure_cookie(request))
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    return response


class HTMLRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def handle(request):
            try:
                return await handler(request)
            except (NotFound, StaleVersion, InvalidState, InvalidInput, HTTPException, RequestValidationError) as error:
                if isinstance(error, HTTPException):
                    status = error.status_code
                elif isinstance(error, NotFound): status = 404
                elif isinstance(error, (StaleVersion, InvalidState)): status = 409
                else: status = 422
                message = ('This request changed. Return to your order link, edit and review it again.'
                           if isinstance(error, StaleVersion) else {
                               401: 'Please sign in as the seller.', 403: 'This form expired. Reload and try again.',
                               404: 'This link or record is unavailable.', 409: 'This action is not available in the current state.',
                               422: 'Check the fields and try again.',
                           }.get(status, 'The request could not be completed.'))
                return render(request, 'error.html', status=status, message=message)
            except Exception:
                return render(request, 'error.html', status=503, message='The service could not complete this action. Check its recorded status before retrying.')
        return handle


router = APIRouter(route_class=HTMLRoute)


def service(request: Request) -> RefundService:
    return request.app.state.service


async def form(request: Request):
    data = await request.form()
    expected = request.cookies.get('daybreak_csrf', '')
    supplied = data.get('csrf', '')
    if not expected or not isinstance(supplied, str) or not hmac.compare_digest(expected, supplied):
        raise HTTPException(403)
    return data


def text(data, name, limit=8000):
    value = data.get(name, '')
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise InvalidInput(name)
    return value.strip()


def cents(value):
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0 or amount > 1000000 or amount * 100 != (amount * 100).to_integral_value():
            raise InvalidInput('amount')
        return int(amount * 100)
    except (InvalidOperation, ValueError):
        raise InvalidInput('amount') from None


@router.get('/static/daybreak.css')
async def css():
    return FileResponse(ROOT / 'static/daybreak.css', media_type='text/css')


@router.get('/')
async def home():
    return RedirectResponse('/seller', status_code=303)


@router.get('/order/{token}')
async def customer(request: Request, token: str, order_id=Depends(require_order)):
    return render(request, 'request.html', token=token, draft=None, case=None)


@router.post('/order/{token}/message')
async def message(request: Request, token: str, order_id=Depends(require_order), data=Depends(form), svc=Depends(service)):
    message_text = text(data, 'message')
    draft_id = data.get('draft_id')
    draft = (await svc.edit_message(text(data, 'draft_id', 200), order_id, message_text)
             if draft_id else await svc.submit_message(order_id, message_text))
    return render(request, 'request.html', token=token, draft=draft, case=None)


@router.post('/order/{token}/confirm')
async def confirm(request: Request, token: str, order_id=Depends(require_order), data=Depends(form), svc=Depends(service)):
    try:
        version = int(text(data, 'draft_version', 10))
    except ValueError:
        raise InvalidInput('version') from None
    case = await svc.confirm(text(data, 'draft_id', 200), version, order_id)
    return render(request, 'request.html', token=token, draft=None, case=case)


@router.get('/login')
async def login_page(request: Request):
    return render(request, 'login.html')


@router.post('/login')
async def login(request: Request, data=Depends(form)):
    if not check_seller_password(text(data, 'password', 1024)):
        raise HTTPException(401)
    response = RedirectResponse('/seller', status_code=303)
    response.set_cookie(SELLER_COOKIE, make_seller_cookie(), httponly=True, samesite='strict',
                        secure=secure_cookie(request), max_age=SELLER_SESSION_SECONDS)
    return response


@router.post('/logout', dependencies=[Depends(require_seller)])
async def logout(data=Depends(form)):
    response = RedirectResponse('/login', status_code=303)
    response.delete_cookie(SELLER_COOKIE)
    return response


@router.get('/seller', dependencies=[Depends(require_seller)])
async def dashboard(request: Request, svc=Depends(service)):
    return render(request, 'dashboard.html', summary=svc.morning_summary(), cases=svc.list_cases(),
                  delegation_id=request.cookies.get('daybreak_delegation', ''))


@router.get('/seller/cases/{request_id}', dependencies=[Depends(require_seller)])
async def detail(request: Request, request_id: str, svc=Depends(service)):
    case = svc.get_case(request_id)
    if case is None: raise NotFound('request')
    return render(request, 'detail.html', case=case)


@router.post('/seller/cases/{request_id}/approve', dependencies=[Depends(require_seller)])
async def approve(request_id: str, data=Depends(form), svc=Depends(service)):
    await svc.approve(request_id)
    return RedirectResponse(f'/seller/cases/{request_id}', status_code=303)


@router.post('/seller/cases/{request_id}/reconcile', dependencies=[Depends(require_seller)])
async def reconcile(request_id: str, data=Depends(form), svc=Depends(service)):
    case = svc.get_case(request_id)
    if not case or not case.job_id: raise NotFound('job')
    await svc.reconcile_job(case.job_id)
    return RedirectResponse(f'/seller/cases/{request_id}', status_code=303)


@router.post('/seller/delegations', dependencies=[Depends(require_seller)])
async def start_delegation(request: Request, data=Depends(form), svc=Depends(service)):
    try:
        expiry = datetime.fromisoformat(text(data, 'expires_at', 50).replace('Z', '+00:00'))
    except ValueError:
        raise InvalidInput('expiry') from None
    if expiry.tzinfo is None: raise InvalidInput('expiry requires timezone')
    delegation_id = await svc.start_delegation(cents(text(data, 'per_refund', 20)), cents(text(data, 'total', 20)), expiry)
    response = RedirectResponse('/seller', status_code=303)
    # shortcut: service has no delegation read view; remember the last created ID in this browser until it does.
    response.set_cookie('daybreak_delegation', delegation_id, httponly=True, samesite='strict', secure=secure_cookie(request))
    return response


@router.post('/seller/delegations/stop', dependencies=[Depends(require_seller)])
async def stop_delegation(data=Depends(form), svc=Depends(service)):
    await svc.stop_delegation(text(data, 'delegation_id', 200))
    return RedirectResponse('/seller', status_code=303)
