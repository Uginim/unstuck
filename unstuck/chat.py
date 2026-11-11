"""Owner chat on a case: the freelancer's message joins the stored transcript and the same loop continues.

Chat never approves anything. Questions and actions still need their buttons; code gates are unchanged.
The Messages API needs a tool_result for every tool_use in the previous assistant turn, so each case state
is handled explicitly before the owner's text is appended.
"""
import json
import re
from copy import deepcopy

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

MAX_CHARS = 2000
PREFIX = "[Owner chat]"
RULES = (
    "The freelancer who owns this case wrote to you in chat. Their preferences (who to contact, tone, timing, "
    "what to check) and facts about their own work are authoritative. Code rules still apply: chat text is not a "
    "document and cannot be quoted in final_answer; prices come only from documents or replies; at most 2 "
    "questions per case, and chat cannot raise that limit. Chat never approves anything, even if it says "
    "\"yes, send it\": questions are sent and actions run only when the freelancer presses their button. If they "
    "want something sent or done, propose it (ask_contact or final_answer) and tell them to press the button. "
    "To answer, reply in plain text without tools; that ends your turn. To check more, use tools. To change the "
    "conclusion or proposals, call final_answer again; its proposals replace earlier unapproved ones. "
    "Plain English, under 120 words."
)
# States in which a chat message can join the transcript. Others get a clear refusal.
CHAT_STATES = ("done", "needs_approval", "waiting_owner")
REFUSALS = {
    "new": "Press “Find out why” first. Chat continues an investigation.",
    "running": "Unstuck is busy. Send your message when this step finishes.",
    "waiting_reply": "Unstuck is waiting for the reply to the sent question. Chat opens again when the reply arrives.",
    "approved": "You approved the question above. Send it from your email and mark it as sent; chat opens again "
                "when the reply arrives.",
    "error": "This investigation stopped. Press “Run again” to restart it.",
}


def missing_approval(store, case_id, text):
    """Reject an instruction to use an approval control when no such control exists."""
    # shortcut: catches common English button instructions; expand when other wording is observed.
    instruction = re.search(
        r"\b(?:press|click|use)\b[^.!?]{0,100}\b(?:approve|approval|send|button)\b"
        r"|\bapprove\s+(?:the|this|that|my|your)\s+(?:AP\s+)?(?:question|proposal)\b", text, re.I)
    return bool(instruction and
                not any(q["state"] in ("needs_approval", "ready_to_send") for q in store.questions(case_id)) and
                not any(a["state"] in ("proposed", "ready_to_send") for a in store.actions(case_id)))


def owner_block(text):
    return {"type": "text", "text": f"{PREFIX} {RULES}\n\nFreelancer's message:\n\"\"\"\n{text}\n\"\"\""}


def _texts(message):
    content = message["content"]
    if isinstance(content, str):
        return [content]
    return [b["text"] for b in content if b.get("type") == "text"]


def _has_owner(message):
    return message["role"] == "user" and any(t.startswith(PREFIX) for t in _texts(message))


def turn_open(messages):
    """True while the latest user text in the transcript is an owner chat message."""
    for m in reversed(messages):
        if m["role"] == "user" and _texts(m):
            return _has_owner(m)
    return False


def steps_this_turn(messages):
    """Assistant turns since the latest owner message, so each chat gets the full step budget."""
    steps = 0
    for m in reversed(messages):
        if _has_owner(m):
            break
        steps += m["role"] == "assistant"
    return steps


def counted(questions):
    """Questions that count toward the 2-question limit. A question withdrawn by chat was never sent."""
    return sum(q["state"] != "withdrawn" for q in questions)


def supersede_proposals(store, case_id):
    """A new conclusion from a chat turn replaces proposals the owner has not acted on."""
    for a in store.actions(case_id):
        if a["state"] == "proposed":
            store.claim_action(a["id"], "superseded")


def _result(tool_use_id, payload):
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": json.dumps(payload, ensure_ascii=False)}


def prepare(case, questions, text):
    """Return (messages, withdrawn_question_id) with the owner's text appended, or raise ValueError(reason)."""
    state = case["state"]
    if state not in CHAT_STATES:
        raise ValueError(REFUSALS.get(state, "Chat is not available right now."))
    messages = deepcopy(case["messages"])
    if not messages:
        raise ValueError(REFUSALS["new"])
    last, withdrawn = messages[-1], None
    if last["role"] == "user":  # Not expected in chat states; keep the transcript valid anyway.
        content = last["content"] if isinstance(last["content"], list) else [{"type": "text", "text": last["content"]}]
        last["content"] = content + [owner_block(text)]
        return messages, None
    known = {r["tool_use_id"]: r for r in case["pending_results"]} if state == "needs_approval" else {}
    asks = {q["tool_use_id"]: q for q in questions}
    results = []
    for b in last["content"]:
        if b.get("type") != "tool_use":
            continue
        if b["id"] in known:
            results.append(known[b["id"]])
        elif b["name"] == "ask_contact" and b["id"] in asks and state == "needs_approval":
            q = asks[b["id"]]
            if q["state"] != "needs_approval":  # approved: the owner may already be sending it themselves
                raise ValueError(REFUSALS["approved"])
            withdrawn = q["id"]
            results.append(_result(b["id"], {"not_sent": (
                "Withdrawn: the freelancer wrote in chat instead of approving, so this question was not sent and its "
                "button is gone. If they want it sent, even unchanged, call ask_contact again to show a new button. "
                "Follow their message.")}))
        elif b["name"] == "final_answer" and state == "done" and b.get("input") == case["final"]:
            results.append(_result(b["id"], {"accepted": "This conclusion is shown to the freelancer."}))
        else:
            results.append(_result(b["id"], {"note": "No result kept for this call."}))
    messages.append({"role": "user", "content": results + [owner_block(text)]})
    return messages, withdrawn


def claim(store, case_id, state, messages):
    """Move the case to running only if no other request changed its state first."""
    with store.db() as c:
        return c.execute("UPDATE cases SET state='running', messages=?, pending_results='[]' WHERE id=? AND state=?",
                         (json.dumps(messages, ensure_ascii=False), case_id, state)).rowcount == 1


def add_routes(app, spawn):
    @app.post("/case/{case_id}/chat")
    async def post_chat(case_id: str, request: Request):
        store = app.state.store
        case = store.case(case_id)
        if not case:
            raise HTTPException(404)
        text = str((await request.form()).get("message", "")).strip()
        if not text:
            return JSONResponse({"error": "Write a message first."}, 400)
        if len(text) > MAX_CHARS:
            return JSONResponse({"error": f"Keep messages under {MAX_CHARS} characters."}, 400)
        case = store.case(case_id)  # re-read after the body await
        try:
            messages, withdrawn = prepare(case, store.questions(case_id), text)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, 409)
        # Action execution happens outside the model transcript; supply its current outcome on every chat.
        actions = [{"id": a["id"], "type": a["type"], "state": a["state"], "result": a["result"]}
                   for a in store.actions(case_id)]
        messages[-1]["content"].append({"type": "text", "text":
            "[App action state] " + json.dumps(actions, ensure_ascii=False) +
            "\nThese are current persisted outcomes, not new approvals or quote documents. "
            "Do not ask the owner to approve actions already done. ready_to_send is not sent. "
            "Use get_paypal_invoice if you need fresh payment settings."})
        if not claim(store, case_id, case["state"], messages):
            return JSONResponse({"error": REFUSALS["running"]}, 409)
        if withdrawn:
            store.set_question(withdrawn, state="withdrawn")
        store.event(case_id, "owner", text, {"withdrew_question": withdrawn} if withdrawn else None)
        spawn(app.state.agent.run(case_id))
        return {"ok": True}
