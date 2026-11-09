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

SELLER_REASONS = {
    'ok': 'The request met the refund policy and the required permission.',
    'over_total': 'Eligible, but beyond the automatic allowance available when reviewed.',
    'over_per_refund': 'Eligible, but above the per-refund automatic limit. Seller approval is required.',
    'no_delegation': 'Eligible, but no automatic permission was granted. Seller approval is required.',
    'delegation_expired': 'Eligible, but automatic permission had expired. Seller approval is required.',
    'delegation_stopped': 'Eligible, but automatic processing was stopped. Seller approval is required.',
    'outside_window': 'The request arrived after the refund deadline.',
    'already_refunded': 'This payment has already been refunded. No additional refund was sent.',
    'in_progress': 'An existing refund is still processing. No additional refund was sent.',
    'result_unconfirmed': 'The existing payment result is unconfirmed and needs reconciliation.',
    'insufficient_order_facts': 'The current payment details could not establish refund eligibility.',
    'ambiguous_request': 'The customer’s intent is unclear and needs review.',
    'not_refund_request': 'The message does not contain a clear refund request.',
    'restore_reconciliation_required': 'Automatic processing is paused until restored records are reconciled.',
}
views.env.filters['seller_reason'] = lambda reason: SELLER_REASONS.get(reason, 'This request needs review.')

def timeline_detail(detail):
    reason, separator, rest = detail.partition(' ')
    return SELLER_REASONS[reason] + (' ' + rest if separator else '') if reason in SELLER_REASONS else detail

views.env.filters['timeline_detail'] = timeline_detail


UI_TEXT = {
    'en': {'send': 'Send message', 'refund': 'Confirm refund request', 'move': 'Confirm date change',
           'keep': 'Keep my booking', 'suggested': 'AI suggestion', 'you': 'You',
           'choose': 'Choose what happens next', 'message': 'Your message',
           'confirm': 'Choosing a button confirms that action. Availability is checked again when you choose.',
           'replaced': 'The AI reply was replaced because it did not match the available amounts. Use the options below.',
           'moved': 'Booking moved', 'kept': 'Booking kept', 'no_money': 'No refund was sent.'},
    'ko': {'send': '메시지 보내기', 'refund': '환불 요청 확정', 'move': '일정 변경 확정',
           'keep': '예약 유지', 'suggested': 'AI 제안', 'you': '나',
           'choose': '원하는 처리를 선택하세요', 'message': '문의 내용',
           'confirm': '버튼을 누르면 해당 요청이 확정됩니다. 선택 시 가능 여부를 다시 확인합니다.',
           'replaced': 'AI 답변의 금액이 제공 가능한 선택지와 일치하지 않아 답변을 교체했습니다. 아래 선택지를 확인하세요.',
           'moved': '예약 일정 변경 완료', 'kept': '예약 유지 완료', 'no_money': '환불은 실행되지 않았습니다.'},
}


def language_key(language):
    return 'ko' if str(language).lower().split('-')[0] == 'ko' else 'en'


views.env.filters['case_status'] = lambda state: {'moved': 'Booking moved', 'kept': 'Booking kept'}.get(state, state.replace('_', ' '))

def customer_result(case, order_id, language="en"):
    """Explicit public fields only; never pass seller evidence into a customer template."""
    state = {
        'completed': 'Refund completed', 'paypal_pending': 'Refund pending',
        'unknown': 'Result unconfirmed', 'failed': 'Refund not completed',
        'reserved': 'Refund processing', 'executing': 'Refund processing',
    }.get(case.job_state)
    if case.status in ('moved', 'kept'):
        state = UI_TEXT[language_key(language)][case.status]
    if state is None:
        state = 'Request declined' if case.status == 'rejected_policy' else 'The seller will review your request'
        if case.reason == 'already_refunded': state = 'Already refunded'
    return {'status': state, 'amount_cents': case.amount_cents,
            'order_id': order_id, 'refund_id': case.refund_id, 'no_money': case.status in ('moved', 'kept')}



def secure_cookie(request: Request):
    return request.url.scheme == 'https' or request.url.hostname not in {'localhost', '127.0.0.1', '::1', 'testserver'}


def render(request: Request, page: str, *, status=200, **values):
    csrf = request.cookies.get('daybreak_csrf') or secrets.token_urlsafe(32)
    try:
        require_seller(request.cookies.get(SELLER_COOKIE))
        seller_signed_in = True
    except HTTPException:
        seller_signed_in = False
    language = language_key(values.pop('language', 'en'))
    response = views.TemplateResponse(request=request, name=page, context={
        'language': language, 'ui': UI_TEXT[language], 'mode': request.app.state.paypal_mode, 'csrf': csrf, 'seller_signed_in': seller_signed_in, **values,
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


@router.get('/static/timeline.js')
async def timeline_script():
    return FileResponse(ROOT / 'static/timeline.js', media_type='text/javascript')


@router.get('/')
async def home():
    return RedirectResponse('/seller', status_code=303)


@router.get('/order/{token}')
async def customer(request: Request, token: str, order_id=Depends(require_order)):
    return render(request, 'request.html', token=token, conversation=None, result=None)


@router.post('/order/{token}/message')
async def message(request: Request, token: str, order_id=Depends(require_order), data=Depends(form), svc=Depends(service)):
    message_text = text(data, 'message')
    draft_id = data.get('draft_id')
    conversation = await svc.converse(order_id, message_text, text(data, 'draft_id', 200) if draft_id else None)
    return render(request, 'request.html', token=token, conversation=conversation, result=None,
                  language=conversation.language)



@router.post('/order/{token}/confirm')
async def confirm(request: Request, token: str, order_id=Depends(require_order), data=Depends(form), svc=Depends(service)):
    try:
        version = int(text(data, 'draft_version', 10))
    except ValueError:
        raise InvalidInput('version') from None
    case = await svc.confirm(text(data, 'draft_id', 200), version, order_id)
    return render(request, 'request.html', token=token, conversation=None, result=customer_result(case, order_id))


@router.post('/order/{token}/choose')
async def choose(request: Request, token: str, order_id=Depends(require_order), data=Depends(form), svc=Depends(service)):
    try:
        version = int(text(data, 'draft_version', 10))
    except ValueError:
        raise InvalidInput('version') from None
    case = await svc.choose(text(data, 'draft_id', 200), version, order_id, text(data, 'option_id', 200))
    language = language_key(data.get('language', 'en'))
    return render(request, 'request.html', token=token, conversation=None,
                  result=customer_result(case, order_id, language), language=language)


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
                  delegation=svc.latest_delegation(), auto_paused=svc.auto_paused(),
                  timeline=sorted(svc.timeline(), key=lambda event: event.at))


@router.get('/seller/cases/{request_id}', dependencies=[Depends(require_seller)])
async def detail(request: Request, request_id: str, svc=Depends(service)):
    case = svc.get_case(request_id)
    if case is None: raise NotFound('request')
    return render(request, 'detail.html', case=case, summary=svc.morning_summary())


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
    await svc.start_delegation(cents(text(data, 'per_refund', 20)), cents(text(data, 'total', 20)), expiry)
    return RedirectResponse('/seller', status_code=303)


@router.post('/seller/delegations/stop', dependencies=[Depends(require_seller)])
async def stop_delegation(data=Depends(form), svc=Depends(service)):
    await svc.stop_delegation(text(data, 'delegation_id', 200))
    return RedirectResponse('/seller', status_code=303)


@router.post('/seller/resume', dependencies=[Depends(require_seller)])
async def resume(data=Depends(form), svc=Depends(service)):
    if data.get('reviewed') != 'yes': raise InvalidInput('review reconciliation first')
    svc.resume_auto()
    return RedirectResponse('/seller', status_code=303)
