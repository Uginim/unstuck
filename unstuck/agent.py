"""Investigation loop. The model picks tools; code owns the end-state gate, the question budget and money.

A run pauses when the agent wants to ask someone (the freelancer approves the question first) and resumes
when the reply arrives. The transcript lives in the store, so a paused run survives restarts.
"""
import asyncio
import json
import os
import re
import time
from decimal import Decimal
from pathlib import Path

import httpx

from .scope import SCOPE_SCHEMA, validate_scope, change_drafts
from .invoicing import summarize
from .unbilled import exact_sentence, validate_create, duplicate_item, agreement_refs
from .honesty import draft_claim_error
from . import chat

MODEL = os.environ.get("UNSTUCK_MODEL", "claude-haiku-5-5")
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00)}  # USD / MTok
SYSTEM = Path(__file__).with_name("prompt.md").read_text()
MAX_STEPS = 16
MAX_QUESTIONS = 2
DAILY_USD = float(os.environ.get("UNSTUCK_DAILY_USD", "3"))
STATUSES = ["CONFIRMED", "NEEDS_OWNER", "EXHAUSTED", "NOT_A_BLOCKER"]
ACTION_TYPES = ["send_reminder", "allow_partial_payment", "draft_reply", "create_invoice"]
NORM = re.compile(r"\s+")

ACTION_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["type", "why"],
    "properties": {
        "type": {"type": "string", "enum": ACTION_TYPES},
        "why": {"type": "string"},
        "recipient": {"type": "string", "description": "create_invoice: exact client recipient email."},
        "currency": {"type": "string", "enum": ["USD"]},
        "items": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "required": ["name", "amount_usd", "quote", "quote_doc"], "properties": {
                "name": {"type": "string"}, "amount_usd": {"type": "number"},
                "quote": {"type": "string", "description": "Exact agreement sentence with the item name and USD price."},
                "quote_doc": {"type": "string"}}}},
        "note": {"type": "string", "description": "send_reminder: the note PayPal shows the client."},
        "minimum_amount_usd": {"type": "number", "description": "allow_partial_payment only."},
        "to": {"type": "string", "description": "draft_reply: recipient email."},
        "subject": {"type": "string"},
        "body": {"type": "string", "description": "draft_reply: email body."},
    },
}
FINAL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["status", "cause", "quote", "quote_doc", "open_question", "summary_for_owner", "proposed_actions"],
    "properties": {
        "status": {"type": "string", "enum": STATUSES},
        "cause": {"type": "string"},
        "quote": {"type": "string", "description": "Exact sentence copied from quote_doc that states the cause. Empty for EXHAUSTED."},
        "quote_doc": {"type": "string"},
        "open_question": {"type": "string", "description": "What is still unknown. Empty if nothing."},
        "summary_for_owner": {"type": "string"},
        "proposed_actions": {"type": "array", "items": ACTION_SCHEMA},
        "scope_findings": SCOPE_SCHEMA,
    },
}
TOOLS = [
    {"name": "search_workspace", "description": "Search the freelancer's mail, contracts and work logs for this client. Returns up to 3 ids with excerpts.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "read_document", "description": "Read one mail, contract, work log or reply by id.",
     "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "get_paypal_invoice", "description": "Live state of this case's PayPal invoice: status, amounts paid and due, payments, partial-payment setting.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_client_invoices", "description": "Read all PayPal invoices for this client's recipient, including paid and draft invoices. Required for unbilled checks.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "ask_contact", "description": "Ask one person at the client one question. The freelancer approves it first; the reply comes back as a document.",
     "input_schema": {"type": "object", "required": ["to", "question", "hypothesis", "evidence_ids"], "properties": {
         "to": {"type": "string"}, "question": {"type": "string"},
         "hypothesis": {"type": "string", "description": "The one hypothesis this question tests."},
         "evidence_ids": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "final_answer", "description": "Finish in one of the four states defined in the system prompt.", "input_schema": FINAL_SCHEMA},
]


class BudgetExceeded(RuntimeError):
    pass


class Investigator:
    def __init__(self, store, invoicing, scenarios, log):
        self.store, self.inv, self.log = store, invoicing, log
        self.scen = {s["key"]: s for s in scenarios}
        self.locks = {}

    # ---------- workspace ----------
    def docs(self, case):
        s = self.scen[case["scenario"]]
        out = {d["id"]: d for d in s["docs"]}
        for q in self.store.questions(case["id"]):
            if q["state"] == "answered":
                out[f"reply-{q['id']}"] = {"id": f"reply-{q['id']}", "kind": "reply", "title": f"Reply from {q['to_addr']}",
                                           "text": f"From: {q['to_addr']}\n\n{q['reply']}"}
        out.update({d["id"]: d for d in self.store.documents(case["id"])})
        return out

    def search(self, case, query):
        terms = [t for t in re.split(r"[\s,.:;()\[\]/\"']+", query.lower()) if len(t) > 1]
        hits = []
        for d in self.docs(case).values():
            score = sum((d["title"] + " " + d["text"]).lower().count(t) for t in terms)
            if score:
                hits.append((score, d["id"], d["title"], d["text"][:160].replace("\n", " ")))
        hits.sort(reverse=True)
        return [{"id": i, "title": t, "excerpt": e} for _, i, t, e in hits[:3]]

    # ---------- model ----------
    async def call(self, case_id, messages):
        if self.store.spent_today() >= DAILY_USD:
            raise BudgetExceeded("daily model budget reached")
        body = dict(model=MODEL, max_tokens=2000, system=SYSTEM, tools=TOOLS, messages=messages, output_config={"effort": "low"})
        # Bound this text-only call before sending; the app serializes calls across runs.
        input_bound = len(json.dumps(body, ensure_ascii=True).encode()) + 4096
        pin, pout = PRICES.get(MODEL, PRICES["claude-sonnet-5-5"])
        multiplier = 5 if MODEL == "claude-haiku-5-5" and input_bound > 100000 else 1
        upper_usd = (input_bound * pin + body["max_tokens"] * pout) * multiplier / 1e6
        if self.store.spent_today() + upper_usd > DAILY_USD:
            raise BudgetExceeded("daily model budget cannot cover the next call")
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=90) as http:
            for attempt in range(3):
                r = await http.post("https://api.anthropic.com/v1/messages", json=body, headers={
                    "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"})
                if r.status_code in (429, 500, 529) and attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                r.raise_for_status()
                break
        raw = r.json()
        pin, pout = PRICES.get(MODEL, PRICES["claude-sonnet-5-5"])
        usage = raw["usage"]
        multiplier = 5 if MODEL == "claude-haiku-5-5" and usage["input_tokens"] > 100000 else 1
        usd = (usage["input_tokens"] * pin + usage["output_tokens"] * pout) * multiplier / 1e6
        self.store.add_spend(case_id, usd)
        self.log.write("model_call", case_id=case_id, request=body, response=raw, usd=usd,
                       ms=round((time.monotonic() - started) * 1000))
        return raw

    # ---------- gate ----------
    def gate(self, case, a, questions):
        if not isinstance(a, dict) or a.get("status") not in STATUSES:
            return "Rejected: invalid final status."
        for field in ("cause", "quote", "quote_doc", "open_question", "summary_for_owner"):
            if not isinstance(a.get(field), str):
                return f"Rejected: {field} must be a string."
        if not a["cause"].strip() or not a["summary_for_owner"].strip():
            return "Rejected: cause and summary_for_owner must not be empty."
        if case.get("mode") == "scope":
            try:
                validate_scope(a, self.docs(case))
            except (ValueError, TypeError) as exc:
                return f"Rejected: {exc}"
        status = a["status"]
        quote, doc_id = NORM.sub(" ", a["quote"]).strip(), a["quote_doc"]
        if status != "EXHAUSTED":
            doc = self.docs(case).get(doc_id)
            if not doc or not exact_sentence(doc["text"], quote):
                return (f"Rejected: the quote is not an exact sentence of document '{doc_id}'. Copy the sentence that "
                        "states the cause, or, if no document states it, ask the person who owns that step.")
        elif questions < MAX_QUESTIONS:
            return (f"Rejected: you still have {MAX_QUESTIONS - questions} question(s). Ask the person who owns the "
                    f"unexplained step about: {a.get('open_question') or 'the unconfirmed cause'}.")
        actions = a.get("proposed_actions")
        if not isinstance(actions, list):
            return "Rejected: proposed_actions must be a list."
        if sum(isinstance(act, dict) and act.get("type") == "create_invoice" for act in actions) > 1:
            return "Rejected: combine agreed unbilled items into one invoice proposal."
        for act in actions:
            if not isinstance(act, dict) or act.get("type") not in ACTION_TYPES:
                return "Rejected: unknown action type."
            fields = ["why"]
            if act["type"] == "create_invoice":
                if case.get("mode") != "unbilled" or status != "NEEDS_OWNER":
                    return "Rejected: create_invoice needs an unbilled check and NEEDS_OWNER status."
                try:
                    validate_create(act, self.docs(case), self.scen[case["scenario"]]["recipient"][1])
                except (ValueError, TypeError) as e:
                    return f"Rejected: {e}"
            elif act["type"] == "allow_partial_payment":
                minimum = act.get("minimum_amount_usd")
                if type(minimum) not in (int, float):
                    return "Rejected: minimum_amount_usd must be a positive number in whole cents."
                amount = Decimal(str(minimum))
                if not amount.is_finite() or amount <= 0 or amount * 100 != (amount * 100).to_integral_value():
                    return "Rejected: minimum_amount_usd must be a positive number in whole cents."
            elif act["type"] == "send_reminder":
                fields.append("note")
            elif act["type"] == "draft_reply":
                fields.extend(("to", "body"))
            for field in fields:
                if not isinstance(act.get(field), str) or not act[field].strip():
                    return f"Rejected: {act['type']} needs nonempty {field}."
        outgoing = (change_drafts(a, self.docs(case), self.scen[case["scenario"]]["recipient"][1])
                    if case.get("mode") == "scope" else actions)
        for act in outgoing:
            reason = draft_claim_error(act, self.docs(case), self.store.actions(case["id"]))
            if reason:
                return reason
        return None

    async def check_unbilled(self, case, actions):
        recipient = self.scen[case["scenario"]]["recipient"][1]
        invoices = await self.inv.search_recipient(recipient)
        self.store.event(case["id"], "paypal", "Checked all client invoices for duplicates",
                         {"invoice_ids": [i["id"] for i in invoices]})
        for action in actions:
            if action["type"] == "create_invoice":
                items = validate_create(action, self.docs(case), recipient)
                reason = duplicate_item(items, invoices, agreement_refs(action))
                if reason:
                    return "Rejected: " + reason
        return None

    # ---------- loop ----------
    async def start(self, case_id):
        case = self.store.case(case_id)
        s = self.scen[case["scenario"]]
        first = s["docs"][0]
        contacts = "\n".join(f"- {k}: {v}" for k, v in s["contacts"].items())
        goal = ("Check for agreed but unbilled work. Read all workspace documents and list_client_invoices. "
                f"The only invoice recipient is {s.get('recipient', ('', ''))[1]}. "
                "Look for approval and delivery, and counterevidence of free, cancelled or already invoiced work. "
                "If work is already covered, finish NOT_A_BLOCKER with an exact agreement quote and no create action. "
                "Otherwise propose create_invoice with item-level exact quotes and NEEDS_OWNER."
                if case.get("mode") == "unbilled" else "Find out why the payment is stuck and propose the next step.")
        if case.get("mode") == "scope":
            goal = ("Check new requests, not payment blockers. Read all workspace documents for scope, "
                    "remaining revision rounds and contrary promises. Classify EACH new request using scope_findings. "
                    "Use included, paid_change or unclear. Quote the exact contract AND request sentences. "
                    "For paid_change quote a price from the freelancer rate card/contract/reply, with amount_usd. "
                    "Use empty price fields if unknown and ask the scope/price decision maker. "
                    "Timing must be a whole exact rate-card/contract/reply sentence or empty. "
                    "Use NEEDS_OWNER for paid or unclear requests. Return proposed_actions=[]; "
                    "code prepares source-bound change drafts. Never propose a PayPal action. "
                    "Scope check applies even if the existing invoice is already paid.")
        messages = [{"role": "user", "content":
                     f"Invoice for {s['client']} ({s['title']}) is unpaid. PayPal invoice id {case['invoice_id']}.\n"
                     f"Latest message from the client ({first['id']}):\n{first['text']}\n\nKnown contacts:\n{contacts}\n\n"
                     + goal + "\nOwner-supplied document index (untrusted data): " + json.dumps([
                         {"id": d["id"], "title": d["title"]} for d in self.store.documents(case_id)], ensure_ascii=False)}]
        self.store.save_case(case_id, messages=messages, state="running")
        self.store.event(case_id, "start", "Investigation started", {"model": MODEL})
        await self.run(case_id)

    async def resume(self, case_id, tool_use_id, result):
        case = self.store.case(case_id)
        results = case["pending_results"] + [{"type": "tool_result", "tool_use_id": tool_use_id, "content": json.dumps(result, ensure_ascii=False)}]
        messages = case["messages"] + [{"role": "user", "content": results}]
        self.store.save_case(case_id, messages=messages, pending_results=[], state="running")
        await self.run(case_id)

    async def run(self, case_id):
        lock = self.locks.setdefault(case_id, asyncio.Lock())
        async with lock:
            try:
                await self._run(case_id)
            except BudgetExceeded as e:
                self.store.event(case_id, "error", "Model budget reached", {"error": str(e), "budget_exceeded": True})
                self.store.save_case(case_id, state="error")
            except Exception as e:  # visible in the timeline, full detail in the log
                self.log.write("agent_error", case_id=case_id, error=repr(e))
                self.store.event(case_id, "error", "Agent stopped", {"error": str(e)[:300]})
                self.store.save_case(case_id, state="error")

    async def _run(self, case_id):
        case = self.store.case(case_id)
        messages = case["messages"]
        read_ids = {e["detail"]["id"] for e in self.store.events(case_id) if e["kind"] == "read"}
        rejections = sum(1 for e in self.store.events(case_id) if e["kind"] == "gate")
        steps = chat.steps_this_turn(messages)
        while steps < MAX_STEPS:
            steps += 1
            raw = await self.call(case_id, messages)
            content = raw["content"]
            messages.append({"role": "assistant", "content": content})
            # In a chat turn, a text-only answer is the agent's reply to the freelancer, not a working note.
            replying = chat.turn_open(messages[:-1]) and not any(b.get("type") == "tool_use" for b in content)
            # A text-only chat reply cannot create the approval button it promises.
            chat_text = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
            if replying and chat.missing_approval(self.store, case_id, chat_text):
                self.store.event(case_id, "gate", "Chat referred to a missing approval button",
                                 {"reason": "Create a proposal with a tool before referring to its approval button."})
                messages.append({"role": "user", "content": [{"type": "text", "text": chat.PREFIX +
                    " [Code validation] There is no approval button. If you want the owner to approve a question, "
                    "call ask_contact now. For an action, call final_answer. Otherwise explain that no proposal "
                    "is available without directing the owner to a nonexistent button. Nothing has been sent."}]})
                self.store.save_case(case_id, messages=messages)
                continue
            for b in content:
                if b.get("type") == "text" and b["text"].strip():
                    if replying:
                        self.store.event(case_id, "agent", b["text"].strip()[:chat.MAX_CHARS])
                    else:
                        self.store.event(case_id, "thought", b["text"].strip()[:400])
            results, pause = [], None
            asked = chat.counted(self.store.questions(case_id))
            for b in content:
                if b.get("type") != "tool_use":
                    continue
                a, name = b.get("input") or {}, b["name"]
                if name == "search_workspace":
                    out = self.search(case, a.get("query", ""))
                    self.store.event(case_id, "search", f"Searched workspace: “{a.get('query', '')}”", {"hits": [h["id"] for h in out]})
                elif name == "read_document":
                    d = self.docs(case).get(a.get("id", ""))
                    if not d:
                        out = {"error": "unknown id"}
                    elif d["id"] in read_ids:
                        out = {"note": "already read; see the earlier result"}
                    else:
                        read_ids.add(d["id"])
                        out = {"id": d["id"], "title": d["title"], "text": d["text"], "provenance": d.get("provenance", "Workspace document")}
                        self.store.event(case_id, "read", f"Read {d['kind']}: {d['title']}", {"id": d["id"]})
                elif name == "get_paypal_invoice":
                    out = summarize(await self.inv.get(case["invoice_id"]))
                    out.pop("payer_view_url", None)
                    self.store.event(case_id, "paypal", f"Checked PayPal invoice: {out['status']}, "
                                     f"paid ${out['paid_cents'] / 100:,.2f}, due ${(out['due_cents'] or 0) / 100:,.2f}", out)
                elif name == "list_client_invoices":
                    recipient = self.scen[case["scenario"]]["recipient"][1]
                    invoices = await self.inv.search_recipient(recipient)
                    out = [summarize(i) for i in invoices]
                    for item in out:
                        item.pop("payer_view_url", None)
                    self.store.event(case_id, "paypal", "Read all client invoices", {"invoice_ids": [i["id"] for i in invoices]})
                elif name == "ask_contact":
                    if pause or asked >= MAX_QUESTIONS:
                        out = {"error": "question budget used; finish now" if asked >= MAX_QUESTIONS else "one question at a time"}
                    else:
                        qid = self.store.add_question(case_id, b["id"], a.get("to", ""), a.get("question", ""),
                                                      a.get("hypothesis", ""), a.get("evidence_ids", []))
                        asked += 1
                        pause = qid
                        self.store.event(case_id, "ask", f"Wants to ask {a.get('to')}", {"question_id": qid, **a})
                        continue
                elif name == "final_answer":
                    reason = ("Rejected: wait for the pending question approval and reply."
                              if pause else self.gate(case, a, asked))
                    if (not reason and chat.turn_open(messages[:-1]) and not a.get("proposed_actions")
                            and not a.get("scope_findings") and chat.missing_approval(self.store, case_id,
                                str(a.get("summary_for_owner", "")))):
                        reason = "Rejected: no question approval button exists. Call ask_contact to propose the question first."
                    if not reason and case.get("mode") == "unbilled":
                        reason = await self.check_unbilled(case, a["proposed_actions"])
                    if reason:
                        rejections += 1
                        out = {"error": reason}
                        self.store.event(case_id, "gate", "Finish rejected by the stop rule", {"reason": reason, "attempt": a})
                    else:
                        self.finish(case_id, messages, a)
                        return
                else:
                    out = {"error": "unknown tool"}
                results.append({"type": "tool_result", "tool_use_id": b["id"], "content": json.dumps(out, ensure_ascii=False)})
            if pause:
                self.store.save_case(case_id, messages=messages, pending_results=results, state="needs_approval")
                return
            if not results and replying:
                self.store.save_case(case_id, messages=messages, state="done" if case["final"] else "waiting_owner")
                return
            if not results:
                messages.append({"role": "user", "content": "Use the tools, then call final_answer."})
            else:
                messages.append({"role": "user", "content": results})
            self.store.save_case(case_id, messages=messages)
        self.store.event(case_id, "error", "Step limit reached without an answer")
        self.store.save_case(case_id, messages=messages, state="error")

    def finish(self, case_id, messages, a):
        case = self.store.case(case_id)
        actions = a.get("proposed_actions", [])
        if case.get("mode") == "scope":
            actions = change_drafts(a, self.docs(case), self.scen[case["scenario"]]["recipient"][1])
        if chat.turn_open(messages):
            chat.supersede_proposals(self.store, case_id)
        for act in actions:
            self.store.add_action(case_id, act["type"], act)
        self.store.event(case_id, "final", f"Finished: {a['status']}", a)
        self.store.save_case(case_id, messages=messages, final=a, state="done")
