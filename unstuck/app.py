"""Unstuck web app: invoice list, live investigation, approvals, and a demo client inbox."""
import asyncio
import math
import os
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from daybreak.logbook import Logbook
from daybreak.paypal import PayPalClient
from .agent import BudgetExceeded, DAILY_USD, Investigator
from .invoicing import Invoicing, InvoicingError, summarize
from .scenarios import FREELANCER, SCENARIOS
from .store import Store
from .brief_email import money_brief, question_links, action_links
from .unbilled import validate_create, duplicate_item, agreement_refs
from .scope import change_drafts
from . import chat
from . import workspaces

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
TASKS = set()
BUDGET_MESSAGE = "Today's demo AI budget is used up. You can still browse existing results. Please try again tomorrow (UTC)."


class DemoInvestigator(Investigator):
    """Serialize model calls so concurrent visitors cannot race the daily spend check."""
    def __init__(self, *args):
        super().__init__(*args)
        self.call_lock = asyncio.Lock()

    async def call(self, case_id, messages):
        async with self.call_lock:
            try:
                return await super().call(case_id, messages)
            except BudgetExceeded as exc:
                raise BudgetExceeded(BUDGET_MESSAGE) from exc


def limit_response(request, message, status=429, retry_after=None):
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(f"<h1>Demo temporarily limited</h1><p>{message}</p><p><a href='/'>Back to demo</a></p>",
                            status_code=status, headers=headers)
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def spawn(coro):
    t = asyncio.create_task(coro)
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)


async def execute_action(app, action):
    """Re-read PayPal, refuse anything stale, run one approved action, read PayPal again."""
    store, inv = app.state.store, app.state.inv
    case = store.case(action["case_id"])
    p = action["params"]
    if case.get("mode") == "scope":
        final = case.get("final")
        if not final or action["type"] != "draft_reply":
            return "refused", {"reason": "Scope checks cannot execute PayPal actions."}
        reason = app.state.agent.gate(case, final, len(store.questions(case["id"])))
        recipient = app.state.agent.scen[case["scenario"]]["recipient"][1]
        if reason or p not in change_drafts(final, app.state.agent.docs(case), recipient):
            return "refused", {"reason": reason or "Draft no longer matches its quoted evidence."}
        return "done", {"note": "Change agreement draft ready for owner review. No email sent or PayPal action taken."}
    before = summarize(await inv.get(case["invoice_id"]))
    if action["type"] == "create_invoice":
        # Serialize different proposals for the same recipient as well as double clicks.
        async with app.state.invoice_create_lock:
            scenario = app.state.agent.scen[case["scenario"]]
            final = case["final"]
            if not final or p not in final.get("proposed_actions", []):
                return "refused", {"reason": "Proposal is not part of the checked final answer."}
            reason = app.state.agent.gate(case, final, len(store.questions(case["id"])))
            if reason:
                return "refused", {"reason": reason}
            items = validate_create(p, app.state.agent.docs(case), scenario["recipient"][1])
            reason = duplicate_item(items, await inv.search_recipient(p["recipient"]), agreement_refs(p))
            if reason:
                return "refused", {"reason": reason}
            request_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                f"unstuck:{case['id']}:{case['invoice_id']}:{action['id']}"))
            new = await inv.create_and_send(number="UB-" + request_id.replace("-", "")[:20],
                recipient_email=p["recipient"], recipient_name=scenario["recipient"][0], items=items,
                note=p["note"], currency="USD", request_id=request_id, references=agreement_refs(p))
            result = summarize(new)
            if (result["recipient"] != p["recipient"] or result["currency"] != "USD"
                    or result["total_cents"] != sum(amount for _, amount in items)
                    or [(i["name"], i["unit_cents"]) for i in result["items"]] != items
                    or result["status"] == "DRAFT" or not result["payer_view_url"]):
                raise RuntimeError("Created invoice could not be verified; review PayPal before any retry")
            new_case_id = "inv-" + new["id"]
            if not store.case(new_case_id):
                store.add_case(new_case_id, case["scenario"], new["id"], case["client"], "Agreed extra work")
            return "done", {"paypal": "POST invoices → POST send → GET invoice", "invoice": result,
                            "case_id": new_case_id, "request_id": request_id}
    if before["status"] in ("PAID", "MARKED_AS_PAID", "CANCELLED", "REFUNDED"):
        return "refused", {"reason": f"invoice is {before['status']}; nothing to do", "invoice": before}
    if action["type"] == "send_reminder":
        if not before["due_cents"]:
            return "refused", {"reason": "nothing is due", "invoice": before}
        await inv.remind(case["invoice_id"], f"Reminder: invoice {before['number']}", p["note"])
        after = summarize(await inv.get(case["invoice_id"]))
        return "done", {"paypal": "POST /v2/invoicing/invoices/{id}/remind", "note": p["note"], "invoice": after}
    if action["type"] == "allow_partial_payment":
        minimum = round(float(p.get("minimum_amount_usd") or 0) * 100)
        if not 0 < minimum < (before["due_cents"] or 0):
            return "refused", {"reason": f"minimum ${minimum / 100:,.2f} must be above 0 and below the amount due", "invoice": before}
        after = await inv.allow_partial(case["invoice_id"], minimum)
        return "done", {"paypal": "PUT /v2/invoicing/invoices/{id} (full replace, items and total verified unchanged)",
                        "invoice": after}
    if action["type"] == "draft_reply":
        return "done", {"note": "Draft ready for the freelancer to send from their own mail."}
    return "refused", {"reason": "unknown action"}


def unbilled_run(app, source, create=False):
    recipient = app.state.agent.scen[source["scenario"]]["recipient"][1]
    return app.state.store.unbilled_case(source, recipient, create=create)


def sweep_state(app, workspace):
    """Each workspace has its own "Check all invoices" run and status line."""
    return app.state.sweeps.setdefault(workspace, {"running": False, "status": "Not started"})


async def sweep(app, allow_start, workspace=""):
    """Sequential, read-only investigations; paused runs are left for their owner."""
    store, agent = app.state.store, app.state.agent
    st = sweep_state(app, workspace)
    st["status"] = "Checking invoices"
    try:
        for row in store.cases(workspace=workspace):
            if store.spent_today() >= DAILY_USD:
                st["status"] = BUDGET_MESSAGE
                return
            live = summarize(await app.state.inv.get(row["invoice_id"]))
            if live["status"] in ("PAID", "MARKED_AS_PAID", "CANCELLED", "REFUNDED") or not (live["due_cents"] or 0) > 0:
                continue
            # A separate run preserves the investigation's pending approval and transcript.
            check = unbilled_run(app, row, create=True)
            for candidate in (row, check):
                current = store.case(candidate["id"])
                if current["state"] != "new":
                    continue
                if store.spent_today() >= DAILY_USD:
                    st["status"] = BUDGET_MESSAGE
                    return
                if not allow_start():
                    st["status"] = "Hourly investigation limit reached. Remaining invoices were not started."
                    return
                if store.claim_case(current["id"]):
                    await agent.start(current["id"])
                    if any(e["kind"] == "error" and (e["detail"] or {}).get("budget_exceeded")
                           for e in store.events(current["id"])):
                        st["status"] = BUDGET_MESSAGE
                        return
        st["status"] = "Checks finished. Review any questions and proposed actions."
    except Exception as exc:
        st["status"] = "Sweep stopped: " + str(exc)[:200]
        app.state.log.write("sweep_error", error=repr(exc))
    finally:
        st["running"] = False


async def periodic_sweep(app, hours, allow_start):
    while True:
        await asyncio.sleep(hours * 3600)
        st = sweep_state(app, "")
        if not st["running"]:
            st["running"] = True
            await sweep(app, allow_start)


async def seed_demo(app):
    """Fresh sandbox invoices for every scenario. Old demo invoices are cancelled where PayPal allows it."""
    store, inv = app.state.store, app.state.inv
    for c in store.cases():
        try:
            await inv.cancel(c["invoice_id"])
        except InvoicingError:
            pass
    store.clear()
    stamp = time.strftime("%m%d%H%M%S")
    for i, s in enumerate(SCENARIOS):
        invoice = await workspaces.create_scenario_invoice(inv, s, f"MP-{stamp}-{i + 1}")
        store.add_case(uuid.uuid4().hex[:10], s["key"], invoice["id"], s["client"], s["title"])


def create_app():
    if not math.isfinite(DAILY_USD) or DAILY_USD < 0:
        raise ValueError("UNSTUCK_DAILY_USD must be a finite non-negative amount")
    log = Logbook(os.environ.get("UNSTUCK_LOG", "logs/unstuck.jsonl"))
    pp = PayPalClient(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"], log=log)
    store = Store(os.environ.get("UNSTUCK_DB", "logs/unstuck.db"))
    inv = Invoicing(pp)
    settings = workspaces.Settings()

    sweep_hours = float(os.environ.get("UNSTUCK_SWEEP_HOURS", "0"))
    if not math.isfinite(sweep_hours) or sweep_hours < 0:
        raise ValueError("UNSTUCK_SWEEP_HOURS must be finite and non-negative")

    @asynccontextmanager
    async def lifespan(app):
        # Public mode seeds per visitor on "Start my demo"; only local mode keeps one shared seeded workspace.
        if not settings.public and not store.cases():
            await seed_demo(app)
        periodic = (asyncio.create_task(periodic_sweep(app, sweep_hours, scheduled_start))
                    if sweep_hours and not settings.public else None)
        try:
            yield
        finally:
            if periodic:
                periodic.cancel()
                with suppress(asyncio.CancelledError):
                    await periodic
            for task in list(TASKS):
                task.cancel()
            if TASKS:
                await asyncio.gather(*list(TASKS), return_exceptions=True)
            await pp.aclose()

    app = FastAPI(title="Unstuck", lifespan=lifespan)
    app.state.store, app.state.inv, app.state.log = store, inv, log
    app.state.agent = DemoInvestigator(store, inv, SCENARIOS, log)
    app.state.invoice_create_lock = asyncio.Lock()
    app.state.sweeps = {}
    workspaces.install(app, settings, templates, lambda coro: spawn(coro))
    # shortcut: process-local counters; use shared limits before adding workers or instances.
    visitors = {}

    scheduled_counts = [time.monotonic(), 0]

    def scheduled_start():
        now = time.monotonic()
        if now - scheduled_counts[0] >= 3600:
            scheduled_counts[:] = [now, 0]
        if scheduled_counts[1] >= 20:
            return False
        scheduled_counts[1] += 1
        return True

    @app.middleware("http")
    async def abuse_limits(request, call_next):
        if request.method != "POST":
            return await call_next(request)
        now = time.monotonic()
        for key, counts in list(visitors.items()):
            if now - counts[0] >= 3600:
                del visitors[key]
        ip = workspaces.client_ip(request)
        if ip not in visitors:
            if len(visitors) >= 10000:
                return limit_response(request, "The demo is busy. Please try again later.", retry_after=3600)
            visitors[ip] = [now, 0, 0]
        counts = visitors[ip]
        path = request.url.path.rstrip("/")
        uses_model = (path.startswith("/case/") and path.endswith(("/investigate", "/unbilled", "/scope", "/chat"))) or path == "/sweep" or (
            path.startswith("/questions/") and path.endswith(("/reply", "/skip")))
        retry = max(1, math.ceil(3600 - (now - counts[0])))
        if counts[1] >= 120 or (uses_model and counts[2] >= 20):
            return limit_response(request, "Too many demo requests from this network. Please try again in an hour.", retry_after=retry)
        counts[1] += 1
        counts[2] += int(uses_model and path != "/sweep")
        def allow_sweep_start():
            if time.monotonic() - counts[0] >= 3600:
                counts[:] = [time.monotonic(), 0, 0]
            if counts[2] >= 20:
                return False
            counts[2] += 1
            return True
        request.state.allow_sweep_start = allow_sweep_start
        if uses_model and store.spent_today() >= DAILY_USD:
            return limit_response(request, BUDGET_MESSAGE)
        return await call_next(request)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    chat.add_routes(app, lambda coro: spawn(coro))
    scen = {s["key"]: s for s in SCENARIOS}

    def need_case(case_id):
        c = store.case(case_id)
        if not c:
            raise HTTPException(404)
        return c

    @app.get("/health")
    def health():
        return {"ok": True, "spent_today_usd": round(store.spent_today(), 4)}

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        gate = workspaces.home_gate(app, request)
        if gate:
            return gate
        ws, rows = request.state.workspace, []
        for c in store.cases(workspace=ws):
            try:
                live = summarize(await inv.get(c["invoice_id"]))
            except InvoicingError:
                live = None
            current = store.case(c["id"])
            check = unbilled_run(app, current)
            def attention(run):
                if not run:
                    return 0
                return (len(store.questions(run["id"], "needs_approval"))
                        + sum(a["state"] == "proposed" for a in store.actions(run["id"])))
            rows.append({**current, "live": live, "unbilled": check, "scope": store.scope_case(current),
                         "attention": attention(current), "unbilled_attention": attention(check)})
        st = sweep_state(app, ws)
        return templates.TemplateResponse(request, "home.html", {"rows": rows, "brief": money_brief(store, rows, workspace=ws), "me": FREELANCER, "sweep_status": st["status"], "sweep_running": st["running"],
            "workspace_expires": workspaces.expires_at(request),
            "refreshing": st["running"] or any(r["state"] == "running" or (r["unbilled"] and r["unbilled"]["state"] == "running") for r in rows)})

    @app.get("/case/{case_id}", response_class=HTMLResponse)
    async def case_page(request: Request, case_id: str):
        c = need_case(case_id)
        s = {**app.state.agent.scen[c["scenario"]], "docs": list(app.state.agent.docs(c).values())}
        return templates.TemplateResponse(request, "case.html", {"case": c, "s": s, "me": FREELANCER})

    @app.post("/case/{case_id}/documents")
    async def add_document(request: Request, case_id: str):
        need_case(case_id)
        form = await request.form()
        try:
            doc_id = store.add_document(case_id, form.get("title"), form.get("text"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, 400)
        store.event(case_id, "document", "Added a document", {"id": doc_id, "provenance": "Added by you"})
        return RedirectResponse("/case/" + case_id, 303)

    @app.get("/api/case/{case_id}")
    async def case_api(case_id: str):
        c = need_case(case_id)
        live = summarize(await inv.get(c["invoice_id"]))
        return {"mode": c["mode"], "state": c["state"], "final": c["final"], "usd": round(c["usd"], 4), "invoice": live,
                "events": store.events(case_id), "questions": [{**q, "compose": question_links(q)} for q in store.questions(case_id)],
                "actions": [{**a, "compose": action_links(a)} for a in store.actions(case_id)]}

    @app.post("/sweep")
    async def start_sweep(request: Request):
        st = sweep_state(app, request.state.workspace)
        if st["running"]:
            return JSONResponse({"error": "A sweep is already running."}, 409)
        st["running"] = True
        spawn(sweep(app, request.state.allow_sweep_start, request.state.workspace))
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/", 303)
        return {"ok": True}

    @app.post("/case/{case_id}/scope")
    async def check_scope(case_id: str):
        check = store.scope_case(need_case(case_id), create=True)
        if store.claim_case(check["id"]):
            spawn(app.state.agent.start(check["id"]))
        return RedirectResponse(f"/case/{check['id']}", 303)

    @app.post("/case/{case_id}/unbilled")
    async def check_unbilled(case_id: str):
        source = need_case(case_id)
        check = unbilled_run(app, source, create=True)
        if store.claim_case(check["id"]):
            spawn(app.state.agent.start(check["id"]))
        return RedirectResponse(f"/case/{check['id']}", 303)

    @app.post("/case/{case_id}/investigate")
    async def investigate(case_id: str):
        c = need_case(case_id)
        if c.get("mode") in ("unbilled", "scope") and c["state"] == "done":
            return JSONResponse({"error": "This client check already exists. Review its questions and proposals."}, 409)
        if any(a["state"] in ("proposed", "running", "ready_to_send") for a in store.actions(case_id)):
            return JSONResponse({"error": "Review pending actions before restarting."}, 409)
        if c["state"] in ("running", "needs_approval", "waiting_reply"):
            return JSONResponse({"error": "already running"}, 409)
        if c["state"] != "new":
            store.save_case(case_id, state="new", final=None, messages=[], pending_results=[])
            with store.db() as db:
                for t in ("events", "questions"):
                    db.execute(f"DELETE FROM {t} WHERE case_id=?", (case_id,))
        store.save_case(case_id, state="running")
        spawn(app.state.agent.start(case_id))
        return {"ok": True}

    @app.post("/questions/{qid}/reply")
    async def reply(qid: int, request: Request):
        q = store.question(qid)
        if not q or q["state"] != "sent":
            raise HTTPException(409)
        form = await request.form()
        text = str(form.get("reply", "")).strip()[:2000]
        if not text:
            raise HTTPException(400)
        with store.db() as db:
            if db.execute("UPDATE questions SET state='answered', reply=? WHERE id=? AND state='sent'", (text, qid)).rowcount != 1:
                raise HTTPException(409)
        store.event(q["case_id"], "reply", f"Reply from {q['to_addr']}", {"question_id": qid, "text": text})
        spawn(app.state.agent.resume(q["case_id"], q["tool_use_id"], {"reply_id": f"reply-{qid}", "from": q["to_addr"], "text": text}))
        return RedirectResponse(f"/case/{q['case_id']}" if form.get("source") == "case" else f"/inbox?submitted={qid}", 303)

    @app.post("/questions/{qid}/{verb}")
    async def question_verb(qid: int, verb: str):
        q = store.question(qid)
        expected = "ready_to_send" if verb == "mark-sent" else "needs_approval"
        if not q or q["state"] != expected or verb not in ("approve", "skip", "mark-sent"):
            raise HTTPException(409)
        target = {"approve": "ready_to_send", "skip": "skipped", "mark-sent": "sent"}[verb]
        if verb == "approve" and not question_links({**q, "state": target}):
            raise HTTPException(400, "Invalid stored email draft")
        with store.db() as db:
            if db.execute("UPDATE questions SET state=? WHERE id=? AND state=?", (target, qid, expected)).rowcount != 1:
                raise HTTPException(409)
        if verb == "mark-sent":
            store.save_case(q["case_id"], state="waiting_reply")
            store.event(q["case_id"], "sent", f"You marked as sent to {q['to_addr']}", {"question_id": qid})
        elif verb == "approve":
            store.event(q["case_id"], "sent", "Ready to send — open your email app", {"question_id": qid})
        else:
            store.event(q["case_id"], "sent", "You chose not to send the question")
            spawn(app.state.agent.resume(q["case_id"], q["tool_use_id"], {"reply": "The freelancer chose not to send this question."}))
        return {"ok": True}

    @app.get("/inbox", response_class=HTMLResponse)
    async def inbox(request: Request):
        items = []
        for q in store.questions(state="sent"):
            c = store.case(q["case_id"])
            if c["workspace"] != request.state.workspace:
                continue
            s = app.state.agent.scen[c["scenario"]]
            items.append({**q, "client": s["client"], "who": s["contacts"].get(q["to_addr"], "Unknown address"),
                          "draft": s["contact_replies"].get(q["to_addr"], "Sorry, I'm not the right person for this.")})
        return templates.TemplateResponse(request, "inbox.html", {"items": items, "me": FREELANCER})

    @app.post("/actions/{aid}/mark-sent")
    async def mark_draft_sent(aid: int):
        with store.db() as db:
            if db.execute("UPDATE actions SET state='sent', result=? WHERE id=? AND type='draft_reply' AND state IN ('ready_to_send','done')",
                          ('{"note":"You marked as sent"}', aid)).rowcount != 1:
                raise HTTPException(409)
        a = store.action(aid)
        store.event(a["case_id"], "action", "You marked as sent", {"action_id": aid})
        return {"ok": True}

    @app.post("/actions/{aid}/{verb}")
    async def action_verb(aid: int, verb: str):
        a = store.action(aid)
        if not a or a["state"] != "proposed" or verb not in ("approve", "dismiss"):
            raise HTTPException(409)
        if verb == "approve" and a["type"] == "draft_reply":
            if store.case(a["case_id"]).get("mode") == "scope":
                state, result = await execute_action(app, a)
                if state != "done":
                    return JSONResponse({"error": result["reason"]}, 409)
            if not action_links({**a, "state": "ready_to_send"}):
                raise HTTPException(400, "Invalid stored email draft")
            if not store.claim_action(aid, "ready_to_send"):
                raise HTTPException(409)
            store.event(a["case_id"], "action", "Ready to send — open your email app", {"action_id": aid})
            return {"ok": True, "state": "ready_to_send"}
        if not store.claim_action(aid, "dismissed" if verb == "dismiss" else "running"):
            raise HTTPException(409)
        if verb == "dismiss":
            store.event(a["case_id"], "action", f"You dismissed: {a['type']}")
            return {"ok": True}
        try:
            state, result = await execute_action(app, a)
        except Exception as e:
            log.write("action_error", action=a, error=repr(e))
            state, result = "failed", {"error": str(e)[:300]}
        store.set_action(aid, state, result)
        store.event(a["case_id"], "action", f"{a['type'].replace('_', ' ').capitalize()}: {state}", result)
        return {"ok": True, "state": state, "result": result}

    @app.post("/demo/reset")
    async def reset(request: Request):
        # A public reset would cancel shared invoices and delete other visitors' active work.
        return limit_response(request, "Reset is disabled for this shared demo so other visitors' work stays safe. "
                              "You can browse existing results or restart a completed investigation.", status=403)

    return app


app = None
if os.environ.get("UNSTUCK_AUTOCREATE", "1") == "1" and os.environ.get("PAYPAL_CLIENT_ID"):
    app = create_app()
