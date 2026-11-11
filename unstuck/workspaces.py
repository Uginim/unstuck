"""Per-visitor demo workspaces for the public URL.

UNSTUCK_MODE=local (default) keeps one shared workspace '' and today's behavior. UNSTUCK_MODE=public gives
each visitor who presses "Start my demo" a private workspace: a signed HttpOnly cookie names it, fresh PayPal
sandbox invoices are created for it in the background, and every id-bearing route answers 404 for ids that
belong to another workspace. Workspace cases carry their workspace in the scenario key ("harbor~<id>"), which
also gives each workspace its own recipient alias ("jordan.lee+<id>@...") so duplicate checks never collide.
"""
import asyncio
import hashlib
import hmac
import ipaddress
import math
import os
import re
import secrets
import time
import uuid
from copy import deepcopy

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from .invoicing import InvoicingError
from .scenarios import FREELANCER, SCENARIOS
from .store import WORKSPACE_SEP, workspace_of

COOKIE = "unstuck_ws"
SCHEMA = """CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY, ip TEXT, created REAL, expires REAL, state TEXT, seeded INTEGER DEFAULT 0, error TEXT);"""
# Paths without a private id: the start page, the read-only sample and static files work without a workspace.
OPEN_PATHS = ("/", "/health", "/sample", "/workspace", "/demo/reset")
ID_PATHS = (
    (re.compile(r"^/(?:api/)?case/([^/]+)"), "case"),
    (re.compile(r"^/questions/(\d+)"), "questions"),
    (re.compile(r"^/actions/(\d+)"), "actions"),
)


class Settings:
    def __init__(self, env=os.environ):
        self.mode = env.get("UNSTUCK_MODE", "local")
        if self.mode not in ("local", "public"):
            raise ValueError("UNSTUCK_MODE must be 'local' or 'public'")
        self.secret = env.get("UNSTUCK_SESSION_SECRET", "")
        if self.public and len(self.secret) < 32:
            raise ValueError("UNSTUCK_SESSION_SECRET must be at least 32 characters in public mode")
        self.per_ip = int(env.get("UNSTUCK_WORKSPACES_PER_IP", "3"))
        self.per_day = int(env.get("UNSTUCK_WORKSPACES_PER_DAY", "60"))
        self.hours = float(env.get("UNSTUCK_WORKSPACE_HOURS", "48"))
        if self.per_ip < 0 or self.per_day < 0 or not math.isfinite(self.hours) or self.hours <= 0:
            raise ValueError("Workspace limits must be non-negative and UNSTUCK_WORKSPACE_HOURS positive")

    @property
    def public(self):
        return self.mode == "public"


def scenario_key(base, workspace):
    return f"{base}{WORKSPACE_SEP}{workspace}" if workspace else base


def alias(email, workspace):
    local, _, domain = email.partition("@")
    return f"{local}+{workspace}@{domain}"


class ScenarioBook(dict):
    """Scenario lookup. 'harbor~<ws>' returns that workspace's own copy with its recipient alias.

    Copies are deep and cached per key, so nothing a workspace does to its scenario reaches another one.
    """
    def __init__(self, base):
        super().__init__(base)
        self._copies = {}

    def __missing__(self, key):
        if key not in self._copies:
            base, _, ws = key.partition(WORKSPACE_SEP)
            if not ws or not dict.__contains__(self, base):
                raise KeyError(key)
            s = deepcopy(dict.__getitem__(self, base))
            name, email = s["recipient"]
            s["recipient"], s["key"] = (name, alias(email, ws)), key
            self._copies[key] = s
        return self._copies[key]


# ---------- signed cookie ----------
def sign(secret, workspace):
    mac = hmac.new(secret.encode(), workspace.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{workspace}.{mac}"


def verify(secret, value):
    workspace = (value or "").partition(".")[0]
    if not re.fullmatch(r"[0-9a-f]{12}", workspace):
        return None
    return workspace if hmac.compare_digest(sign(secret, workspace), value) else None


def client_ip(request):
    ip = request.client.host if request.client else "unknown"
    # Render's edge supplies the first address; never trust this header on a direct local server.
    if os.environ.get("RENDER") == "true":
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        try:
            ip = str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return ip


# ---------- workspace rows ----------
def ensure(store):
    with store.db() as c:
        c.executescript(SCHEMA)


def row(store, workspace):
    with store.db() as c:
        r = c.execute("SELECT * FROM workspaces WHERE id=?", (workspace,)).fetchone()
    return dict(r) if r else None


def set_row(store, workspace, **fields):
    with store.db() as c:
        c.execute(f"UPDATE workspaces SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), workspace))


def created_since(store, since, ip=None):
    q, args = "SELECT COUNT(*) FROM workspaces WHERE created>=?", [since]
    if ip is not None:
        q, args = q + " AND ip=?", args + [ip]
    with store.db() as c:
        return c.execute(q, args).fetchone()[0]


def delete_cases(store, workspace):
    with store.db() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM cases WHERE workspace=?", (workspace,))]
        for t in ("events", "questions", "actions", "documents"):
            c.executemany(f"DELETE FROM {t} WHERE case_id=?", [(i,) for i in ids])
        c.execute("DELETE FROM cases WHERE workspace=?", (workspace,))


def purge_expired(store, now):
    """Expired workspaces are never reused; their rows are deleted. Their sandbox invoices are left as they are."""
    with store.db() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM workspaces WHERE expires<=? AND state!='expired'", (now,))]
    for workspace in ids:
        delete_cases(store, workspace)
        set_row(store, workspace, state="expired")


def status(app, r, now=None):
    """The state a visitor sees: expired by time, and 'failed' if seeding stopped without finishing (e.g. restart)."""
    if not r:
        return None
    if (now or time.time()) >= r["expires"] or r["state"] == "expired":
        return "expired"
    if r["state"] == "seeding" and r["id"] not in app.state.seeding:
        return "failed"
    return r["state"]


# ---------- seeding ----------
async def create_scenario_invoice(inv, s, number):
    """One scenario's sandbox invoice, sent to its (possibly aliased) fictional recipient, with any seed payment."""
    name, email = s["recipient"]
    invoice = await inv.create_and_send(number=number, recipient_email=email, recipient_name=name,
                                        items=s["items"], note=f"{s['title']} - {FREELANCER['name']}",
                                        invoice_date=s["invoice_date"], terms_days=s["terms_days"])
    if s["seed_payment"]:
        sp = s["seed_payment"]
        await inv.record_external_payment(invoice["id"], sp["cents"], sp["date"], sp["note"])
    return invoice


async def seed(app, workspace):
    """Create this workspace's invoices. PayPal calls run one workspace at a time; visitors see progress."""
    store, inv, created = app.state.store, app.state.inv, []
    try:
        async with app.state.seed_lock:
            app.state.seed_active = workspace
            for i, base in enumerate(SCENARIOS):
                s = app.state.agent.scen[scenario_key(base["key"], workspace)]
                invoice = await create_scenario_invoice(inv, s, f"MP-{workspace}-{i + 1}")
                created.append(invoice["id"])
                store.add_case(uuid.uuid4().hex[:10], s["key"], invoice["id"], s["client"], s["title"])
                set_row(store, workspace, seeded=i + 1)
            set_row(store, workspace, state="ready")
    except Exception as exc:
        app.state.log.write("workspace_seed_error", workspace=workspace, error=repr(exc))
        for invoice_id in created:
            try:
                await inv.cancel(invoice_id)
            except InvoicingError:
                pass
        delete_cases(store, workspace)
        set_row(store, workspace, state="failed", error="PayPal sandbox did not accept the demo invoices.")
    finally:
        app.state.seeding.discard(workspace)
        if app.state.seed_active == workspace:
            app.state.seed_active = None


# ---------- request handling ----------
def owner_of(store, path):
    """The workspace owning the id in this path, '' for local rows, None if the path has no id, False if unknown."""
    for pattern, kind in ID_PATHS:
        m = pattern.match(path)
        if not m:
            continue
        with store.db() as c:
            if kind == "case":
                r = c.execute("SELECT workspace FROM cases WHERE id=?", (m[1],)).fetchone()
            else:
                r = c.execute(f"SELECT cases.workspace FROM {kind} JOIN cases ON cases.id={kind}.case_id "
                              f"WHERE {kind}.id=?", (int(m[1]),)).fetchone()
        return r[0] if r else False
    return None


def install(app, settings, templates, spawn):
    app.state.workspace_settings = settings
    app.state.seeding, app.state.seed_active = set(), None
    app.state.seed_lock = asyncio.Lock()
    app.state.agent.scen = ScenarioBook(app.state.agent.scen)
    ensure(app.state.store)

    @app.middleware("http")
    async def workspace_guard(request, call_next):
        request.state.workspace, request.state.workspace_row = "", None
        if not settings.public:
            return await call_next(request)
        store, path = app.state.store, request.url.path.rstrip("/") or "/"
        r = row(store, verify(settings.secret, request.cookies.get(COOKIE)) or "")
        request.state.workspace_row = r
        state = status(app, r)
        request.state.workspace = r["id"] if state == "ready" else None
        if path in OPEN_PATHS or path.startswith("/static/"):
            return await call_next(request)
        if request.state.workspace is None:
            return RedirectResponse("/", 303) if request.method == "GET" else JSONResponse({"error": "Start your demo first."}, 404)
        if owner_of(store, path) not in (None, request.state.workspace):
            return JSONResponse({"detail": "Not Found"}, 404)
        return await call_next(request)

    def page(request, status_code=200, **ctx):
        r = request.state.workspace_row
        ctx.setdefault("state", status(app, r))
        resp = templates.TemplateResponse(request, "start.html", {
            "me": FREELANCER, "row": r, "total": len(SCENARIOS), "hours": settings.hours,
            "queued": bool(r and app.state.seed_active not in (None, r["id"]) and not r["seeded"]), **ctx},
            status_code=status_code)
        if ctx["state"] == "expired":
            resp.delete_cookie(COOKIE, path="/")
        return resp

    app.state.workspace_page = page

    @app.post("/workspace")
    async def start(request: Request):
        if not settings.public:
            return RedirectResponse("/", 303)
        now, store = time.time(), app.state.store
        if status(app, request.state.workspace_row) in ("ready", "seeding"):
            return RedirectResponse("/", 303)
        ip = hashlib.sha256((settings.secret + client_ip(request)).encode()).hexdigest()[:16]
        day = now - 86400
        if created_since(store, day, ip) >= settings.per_ip:
            return page(request, 429, state="limit", reason=f"This network already started {settings.per_ip} demos today.")
        if created_since(store, day) >= settings.per_day:
            return page(request, 429, state="limit", reason="The demo reached today's limit of new visitors.")
        purge_expired(store, now)
        workspace = secrets.token_hex(6)
        with store.db() as c:
            c.execute("INSERT INTO workspaces (id, ip, created, expires, state) VALUES (?,?,?,?,'seeding')",
                      (workspace, ip, now, now + settings.hours * 3600))
        app.state.seeding.add(workspace)
        spawn(seed(app, workspace))
        resp = RedirectResponse("/", 303)
        resp.set_cookie(COOKIE, sign(settings.secret, workspace), max_age=int(settings.hours * 3600), httponly=True,
                        samesite="lax", secure=os.environ.get("RENDER") == "true", path="/")
        return resp

    @app.get("/sample")
    async def sample(request: Request):
        return page(request, state="sample", scenarios=SCENARIOS)


def home_gate(app, request):
    """In public mode, the start, progress, failure or expiry page instead of the invoice list; else None."""
    settings = app.state.workspace_settings
    if not settings.public or request.state.workspace:
        return None
    return app.state.workspace_page(request)


def expires_at(request):
    r = request.state.workspace_row
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(r["expires"])) if r else None
