"""Demo-only identifiers and passwords; no verification or outbound mail."""
import asyncio
import hashlib
import hmac
import re
import secrets
import time
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import RedirectResponse

ITERATIONS = 260_000
SCHEMA = """CREATE TABLE IF NOT EXISTS accounts (
 email TEXT PRIMARY KEY, salt BLOB NOT NULL, hash BLOB NOT NULL,
 workspace TEXT NOT NULL UNIQUE, created REAL NOT NULL);"""


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), salt, ITERATIONS)


def email_for(store, workspace):
    with store.db() as db:
        row = db.execute('SELECT email FROM accounts WHERE workspace=?', (workspace,)).fetchone()
    return row['email'] if row else None


def install(app, page, create_workspace, cookie_response):
    from . import workspaces
    store = app.state.store
    # shortcut: process-local failure limits, matching the single-worker deployment.
    failures = {}

    async def credentials(request):
        form = await request.form()
        email, password = form.get('email'), form.get('password')
        if not isinstance(email, str) or not isinstance(password, str):
            return '', ''
        return email.strip().lower(), password

    def cross_origin(request):
        origin = request.headers.get('origin')
        return origin is not None and urlsplit(origin).netloc != request.url.netloc

    def error(request, message, code=400):
        return page(request, code, state='auth', auth_error=message)

    @app.post('/signup')
    async def signup(request: Request):
        if cross_origin(request):
            return error(request, 'Please use the forms on this site.', 403)
        email, password = await credentials(request)
        if len(email) > 254 or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+', email):
            return error(request, 'Enter an email address.')
        if not 8 <= len(password) <= 1024:
            return error(request, 'Use a password between 8 and 1,024 characters.')
        if request.state.account and workspaces.status(app, request.state.workspace_row) != 'failed':
            return error(request, 'Log out before creating another account.')
        workspaces.purge_expired(store, time.time())
        with store.db() as db:
            exists = db.execute('SELECT workspace FROM accounts WHERE email=?', (email,)).fetchone()
        if exists and workspaces.status(app, workspaces.row(store, exists['workspace'])) == 'failed':
            workspaces.delete_cases(store, exists['workspace'])
            workspaces.set_row(store, exists['workspace'], state='failed')
            exists = None
        if exists:
            return error(request, 'An account with this email already exists. Please log in.')
        salt = secrets.token_bytes(32)
        digest = await asyncio.to_thread(password_hash, password, salt)
        return create_workspace(request, account=(email, salt, digest))

    @app.post('/login')
    async def login(request: Request):
        if cross_origin(request):
            return error(request, 'Please use the forms on this site.', 403)
        now = time.monotonic()
        for ip, (since, _) in list(failures.items()):
            if now - since >= 900:
                del failures[ip]
        ip = workspaces.client_ip(request)
        if len(failures) >= 10000 and ip not in failures:
            return error(request, 'Login is busy. Please try again in 15 minutes.', 429)
        count = failures.setdefault(ip, [now, 0])
        if count[1] >= 10:
            return error(request, 'Too many failed logins. Please try again in 15 minutes.', 429)
        # Reserve before hashing so concurrent attempts cannot bypass the limit.
        count[1] += 1
        email, password = await credentials(request)
        workspaces.purge_expired(store, time.time())
        with store.db() as db:
            account = db.execute('SELECT * FROM accounts WHERE email=?', (email,)).fetchone()
        digest = await asyncio.to_thread(password_hash, password if len(password) <= 1024 else '',
                                        account['salt'] if account else bytes(32))
        valid = hmac.compare_digest(digest, account['hash'] if account else bytes(32))
        if not valid or not account:
            return error(request, 'Email or password is wrong.')
        workspace = workspaces.row(store, account['workspace'])
        if not workspace or workspaces.status(app, workspace) == 'expired':
            return error(request, 'Email or password is wrong.')
        count[1] -= 1
        return cookie_response(request, workspace['id'], workspace['expires'])

    @app.post('/logout')
    async def logout(request: Request):
        if cross_origin(request):
            return error(request, 'Please use the forms on this site.', 403)
        response = RedirectResponse('/', 303)
        response.delete_cookie(workspaces.COOKIE, path='/', httponly=True, samesite='lax',
                               secure=request.url.scheme == 'https' or workspaces.os.environ.get('RENDER') == 'true')
        return response
