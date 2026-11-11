const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const usd = (c) => "$" + ((c || 0) / 100).toLocaleString("en-US", {minimumFractionDigits: 2});
const words = (s) => String(s ?? "").replace(/_/g, " ");

const STATE = {new: "Not started", running: "Investigating", needs_approval: "Needs your approval",
  waiting_reply: "Waiting for reply", waiting_owner: "Waiting for you", done: "Done", error: "Error"};
const STATUS = {CONFIRMED: ["Cause confirmed", "ok"], NEEDS_OWNER: ["Cause confirmed · your decision needed", "ok"],
  EXHAUSTED: ["Cause not confirmed", "warn"], NOT_A_BLOCKER: ["Not stuck", "ok"]};
// Unbilled and scope checks reuse the investigation statuses, but "Not stuck" / "Cause confirmed" misread there.
function statusTitle(mode, status) {
  if (mode === "scope") return "New requests classified";
  if (mode === "unbilled" && status === "NOT_A_BLOCKER") return "Nothing unbilled found";
  if (mode === "unbilled" && status === "NEEDS_OWNER") return "Your decision needed";
  return (STATUS[status] || [words(status)])[0];
}
const ACTION = {send_reminder: "Send a PayPal reminder", allow_partial_payment: "Allow partial payment",
  create_invoice: "Create and send an agreed-work invoice", draft_reply: "Draft a reply email"};
const ACTION_STATE = {proposed: "Needs your approval", running: "Running", done: "Done", refused: "Refused",
  failed: "Failed", dismissed: "Dismissed", ready_to_send: "Ready to send", sent: "You marked as sent", superseded: "Replaced"};
const WAITING = {
  new: "Press “Find out why” to start. The answer shows up here.",
  running: "Investigating. The answer shows up here.",
  needs_approval: "Waiting for you to approve or skip a question.",
  waiting_reply: "Waiting for a reply to the question.",
  error: "This run stopped. Use the button at the top to retry.",
  waiting_owner: "Unstuck answered in chat. Write again, or press “Run again” to restart.",
};

const svg = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const ICON = {
  start: svg('<path d="M8 5v14l11-7z" fill="currentColor" stroke="none"/>'),
  paypal: "<b>P</b>",
  search: svg('<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4-4"/>'),
  read: svg('<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>'),
  thought: svg('<circle cx="6" cy="12" r="1.3" fill="currentColor"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/><circle cx="18" cy="12" r="1.3" fill="currentColor"/>'),
  ask: svg('<path d="M20 12a8 8 0 0 1-11.6 7.1L4 20l1-4.4A8 8 0 1 1 20 12z"/><path d="M9.8 9.6a2.3 2.3 0 0 1 4.4.9c0 1.5-2.2 1.8-2.2 3"/><path d="M12 16.2h.01"/>'),
  sent: svg('<path d="M21 3 10 14"/><path d="M21 3 14.5 21 10 14 3 9.5z"/>'),
  reply: svg('<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 7 8.5 6 8.5-6"/>'),
  gate: svg('<path d="M12 3 5 6v5.5c0 4.4 3 7.8 7 9.5 4-1.7 7-5.1 7-9.5V6z"/><path d="M9.5 12h5"/>'),
  final: svg('<path d="m5 12.5 4.5 4.5L19 7.5"/>'),
  action: svg('<path d="M13 3 5 13.5h6L10 21l8-10.5h-6z"/>'),
  error: svg('<circle cx="12" cy="12" r="8.5"/><path d="M12 8v4.5M12 16h.01"/>'),
  owner: svg('<circle cx="12" cy="8" r="3.5"/><path d="M5 20a7 7 0 0 1 14 0"/>'),
  agent: svg('<path d="M20 12a8 8 0 0 1-11.6 7.1L4 20l1-4.4A8 8 0 1 1 20 12z"/>'),
};
const TONE = {paypal: "check", search: "check", read: "check", ask: "people", sent: "people", reply: "people", owner: "chat", agent: "chat"};
// Why chat is closed in each case state; mirrors unstuck/chat.py refusals.
const CHAT_HINT = {
  new: "Press “Find out why” first.", running: "Unstuck is working…",
  waiting_reply: "Chat opens again when the reply arrives.", error: "Use the button at the top to retry.",
};

let timer = null, lastKey = "", seen = null, hadFinal = null;

async function post(url) {
  const r = await fetch(url, {method: "POST"});
  if (!r.ok) alert((await r.json().catch(() => ({}))).error || "Request failed");
  refresh();
}

// Titles of workspace documents and replies, so the page can name sources instead of ids.
function docTitle(id, questions) {
  const li = document.querySelector(`.docs li[data-doc="${CSS.escape(id || "")}"]`);
  if (li) return `${li.dataset.title} (${li.dataset.provenance || li.dataset.kind})`;
  const m = /^reply-(\d+)$/.exec(id || "");
  const q = m && questions.find((x) => x.id === Number(m[1]));
  return q ? `Reply from ${q.to_addr}` : id;
}

function elapsed(at, t0) {
  const s = Math.max(0, Math.round(at - t0));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

function invoiceHtml(i) {
  const pays = i.payments.map((p) => `<li>${usd(p.cents)} · ${esc(words(p.method).toLowerCase())} · ${esc(p.date)}</li>`).join("");
  return `<div class="amount"><span class="money">${usd(i.due_cents)}</span><span class="muted">due</span></div>
    <dl><dt>Status</dt><dd><b>${esc(words(i.status))}</b></dd><dt>Total</dt><dd>${usd(i.total_cents)}</dd>
    <dt>Paid</dt><dd>${usd(i.paid_cents)}</dd><dt>Due date</dt><dd>${esc(i.due_date)}</dd>
    <dt>Partial pay</dt><dd>${i.allow_partial_payment ? "allowed, min " + usd(i.minimum_due_cents) : "off"}</dd>
    <dt>Reminders</dt><dd>${i.reminders_sent == null ? "History not provided by PayPal" : esc(i.reminders_sent) + " sent"}</dd>
    <dt>Number</dt><dd>${esc(i.number)}</dd></dl>${pays ? `<div class="k">Payments</div><ul class="pays">${pays}</ul>` : ""}`;
}

function composeHtml(links, path) {
  if (!links) return '<p class="small">Compose link unavailable: review the stored recipient and subject.</p>';
  return `<div class="btns"><a href="${esc(links.gmail)}" target="_blank" rel="noopener noreferrer">Open in Gmail</a>
    <a href="${esc(links.mailto)}">Open default mail app</a>
    <button onclick="this.disabled=true;post('${path}/mark-sent')">I sent it</button></div>
    <p class="small muted">Ready to send. Send from your email app, then mark it here. Unstuck does not send or verify delivery.</p>`;
}

function eventHtml(e, c, t0) {
  const d = e.detail || {};
  let label = esc(e.label), body = "";
  if (e.kind === "ask") {
    const q = c.questions.find((x) => x.id === d.question_id) || {};
    const pending = q.state === "needs_approval";
    body = `<div class="askcard${pending ? " pending" : ""}">
      ${d.hypothesis ? `<div class="suspect"><span class="k">Suspects</span>${esc(d.hypothesis)}</div>` : ""}
      <div class="k">Question to <span class="addr">${esc(d.to)}</span></div><p class="q">“${esc(d.question)}”</p>
      ${pending ? `<div class="btns"><button class="primary big" onclick="this.disabled=true;post('/questions/${q.id}/approve')">Approve question</button>
        <button class="ghost" onclick="this.disabled=true;post('/questions/${q.id}/skip')">Don't send</button></div>
        <div class="small muted">Approval prepares a compose link. You send from your email app.</div>` : ""}
      ${q.state === "ready_to_send" ? composeHtml(q.compose, '/questions/' + q.id) : ""}
      ${q.state === "sent" ? `<p class="small muted">You marked as sent. Waiting for a reply.</p>
        <form method="post" action="/questions/${q.id}/reply"><input type="hidden" name="source" value="case">
        <label for="paste-${q.id}">Paste the client's reply</label><textarea id="paste-${q.id}" name="reply" rows="5" maxlength="2000" required></textarea>
        <button class="primary">Submit reply and continue</button></form>
        <a class="small" href="/inbox">Simulated client inbox (demo)</a>` : ""}
      ${q.state === "skipped" ? `<div class="small muted">Not sent.</div>` : ""}
      ${q.state === "withdrawn" ? `<div class="small muted">Not sent. You wrote in chat instead.</div>` : ""}</div>`;
  } else if (e.kind === "search") {
    const hits = d.hits || [];
    body = `<div class="sub">${hits.length ? `Found: ${hits.map((h) => esc(docTitle(h, c.questions))).join(" · ")}` : "No matches"}</div>`;
  } else if (e.kind === "reply") body = `<blockquote>${esc(d.text)}</blockquote>`;
  else if (e.kind === "gate") body = gateBody(d);
  else if (e.kind === "thought") label = `<span class="k">Thinking</span>${esc(e.label)}`;
  else if (e.kind === "owner") label = `<span class="k">You</span><div class="bubble">${esc(e.label)}</div>`;
  else if (e.kind === "agent") label = `<span class="k">Unstuck</span><div class="bubble">${esc(e.label)}</div>`;
  else if (e.kind === "final") label = `Finished: ${esc(statusTitle(c.mode, d.status))}`;
  else if (e.kind === "action") label = esc(words(e.label));
  else if (e.kind === "error") body = d.error ? `<div class="sub bad">${esc(d.error)}</div>` : "";
  const fresh = seen && !seen.has(e.id) ? " enter" : "";
  return `<li class="ev ev-${esc(e.kind)}${TONE[e.kind] ? " tone-" + TONE[e.kind] : ""}${fresh}"><span class="node">${ICON[e.kind] || "•"}</span>
    <div class="ev-b"><div class="ev-h"><div class="lbl">${label}</div><span class="t">${elapsed(e.at, t0)}</span></div>${body}</div></li>`;
}

function gateBody(d) {
  const tried = d.attempt && typeof d.attempt.cause === "string" && d.attempt.cause.trim();
  return `<div class="gatebox">${tried ? `<div><span class="k">Answer it tried</span>${esc(tried)}</div>` : ""}
    <div class="why">${esc(d.reason)}</div></div>`;
}

// Back-to-back stop-rule rejections read as one calm line; each reason stays one click away.
function timelineHtml(c, t0) {
  const out = [];
  let run = [];
  const flush = () => {
    if (run.length > 1) {
      const last = run[run.length - 1], fresh = seen && run.some((e) => !seen.has(e.id)) ? " enter" : "";
      out.push(`<li class="ev ev-gate${fresh}"><span class="node">${ICON.gate}</span><div class="ev-b">
        <div class="ev-h"><div class="lbl">Stop rule sent it back ${run.length} times</div><span class="t">${elapsed(last.at, t0)}</span></div>
        <details class="gaterun"><summary>Show each reason</summary>${run.map((e) => gateBody(e.detail || {})).join("")}</details></div></li>`);
    } else run.forEach((e) => out.push(eventHtml(e, c, t0)));
    run = [];
  };
  for (const e of c.events) {
    if (e.kind === "gate") run.push(e);
    else { flush(); out.push(eventHtml(e, c, t0)); }
  }
  flush();
  return out.join("");
}

function resultHtml(c) {
  const f = c.final;
  if (!f) return `<div class="panel waiting"><div class="k">Answer</div><p>${esc(WAITING[c.state] || "")}</p></div>`;
  if (c.mode === "scope") {
    const labels = {included: "Included revision", paid_change: "Paid change · agreement needed", unclear: "Unclear · confirmation needed"};
    return `<div class="panel result scope-result"><h2>New requests</h2>${(f.scope_findings || []).map(x =>
      `<section><h3>${esc(x.name)}</h3><p><strong>${esc(labels[x.classification])}</strong></p><p>${esc(x.reason)}</p>
      ${["request", "contract", "price", "timing"].filter(k => x[k + "_quote"]).map(k =>
        `<blockquote>${esc(x[k + "_quote"])}<cite>${esc(docTitle(x[k + "_doc"], c.questions))}</cite></blockquote>`).join("")}
      ${x.classification === "paid_change" && !x.price_quote ? '<p class="open-q">Price not confirmed. No draft prepared.</p>' : ""}</section>`).join("")}
      <p>${esc(f.summary_for_owner)}</p>${f.open_question ? `<p class="open-q">${esc(f.open_question)}</p>` : ""}
      <p class="small muted">No invoice or payment change. Agree scope first; check unbilled work after delivery.</p></div>
      <div class="small muted cost">Model cost for this case: $${c.usd.toFixed(4)}</div>`;
  }
  const [, tone] = STATUS[f.status] || [words(f.status), "warn"];
  const title = statusTitle(c.mode, f.status);
  const lead = c.mode === "unbilled" ? "Agreed work and billing" : f.status === "EXHAUSTED" ? "What is known so far" : f.status === "NOT_A_BLOCKER" ? "What the evidence shows" : "Why it's not paid";
  return `<div class="panel result tone-${tone} st-${esc(f.status)}">
    <div class="result-h"><span class="badge">${tone === "ok" ? ICON.final : ICON.error}</span><span>${esc(title)}</span></div>
    <div class="result-b"><div class="k">${lead}</div><p class="cause">${esc(f.cause)}</p>
    ${f.quote ? `<blockquote>“${esc(f.quote)}”<cite>${esc(docTitle(f.quote_doc, c.questions))}</cite></blockquote>` : ""}
    ${f.open_question ? `<p class="open-q"><span class="k">Still open</span>${esc(f.open_question)}</p>` : ""}
    <p class="summary">${esc(f.summary_for_owner)}</p></div></div>
    <div class="small muted cost">Model cost for this case: $${c.usd.toFixed(4)}</div>`;
}

function payerLink(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol === "https:" && (parsed.hostname === "paypal.com" || parsed.hostname.endsWith(".paypal.com")))
      return `<a href="${esc(parsed.href)}" target="_blank" rel="noopener noreferrer">Open PayPal invoice</a>`;
  } catch (_) { /* No link when PayPal did not return a valid payer URL. */ }
  return "";
}

function afterHtml(inv) {
  if (!inv) return "";
  const bits = [words(inv.status), `due ${usd(inv.due_cents)}`, inv.reminders_sent == null ? "reminder history not provided by PayPal" : `${inv.reminders_sent} reminder(s) sent`,
    inv.allow_partial_payment ? `partial pay allowed, min ${usd(inv.minimum_due_cents)}` : "partial pay off"];
  return `<div class="sub">Invoice ${esc(inv.number)} · PayPal invoice now: ${esc(bits.join(" · "))}</div>${payerLink(inv.payer_view_url)}`;
}

function actionHtml(a) {
  const p = a.params;
  let what = "";
  if (a.type === "send_reminder") what = `PayPal reminder on this invoice with note:<blockquote>${esc(p.note)}</blockquote>`;
  if (a.type === "allow_partial_payment") what = `Let the client pay part now. Minimum payment <b>$${Number(p.minimum_amount_usd).toLocaleString("en-US", {minimumFractionDigits: 2})}</b>. Items and total stay the same.`;
  if (a.type === "draft_reply") what = `Email draft to <b>${esc(p.to)}</b><pre>${esc(p.subject ? p.subject + "\n\n" : "")}${esc(p.body)}</pre>`;
  if (a.type === "create_invoice") what = `<p>To <b>${esc(p.recipient)}</b> · USD</p>
    <ul>${p.items.map(i => `<li>${esc(i.name)} — ${usd(Math.round(i.amount_usd * 100))}
    <blockquote>${esc(i.quote)}<cite>${esc(docTitle(i.quote_doc, []))}</cite></blockquote></li>`).join("")}</ul>
    <p>Total: <b>${usd(p.items.reduce((sum, i) => sum + Math.round(i.amount_usd * 100), 0))}</b></p>
    <p>${esc(p.note)}</p><p class="small muted">Approval creates and sends a new Sandbox invoice. Existing invoices stay unchanged.</p>`;
  let res = "";
  if (a.result) {
    const r = a.result;
    res = `<div class="res ${["done", "sent"].includes(a.state) ? "ok" : "bad"}">${esc(r.reason || r.note && a.type === "draft_reply" && r.note || r.error || (a.state === "done" ? "Done" : ""))}
      ${r.paypal ? `<code>PayPal API: ${esc(r.paypal)}</code>` : ""}${a.state === "done" ? afterHtml(r.invoice) : ""}</div>`;
  }
  return `<div class="panel action st-${esc(a.state)}"><div class="action-h"><b>${esc(ACTION[a.type] || words(a.type))}</b>
    <span class="pill st-${esc(a.state)}">${esc(ACTION_STATE[a.state] || a.state)}</span></div>
    <div class="why">${esc(p.why)}</div><div class="what">${what}</div>${res}
    ${a.type === "draft_reply" && ["ready_to_send", "done"].includes(a.state) ? composeHtml(a.compose, "/actions/" + a.id) : ""}
    ${a.state === "proposed" ? `<div class="btns"><button class="primary big" onclick="this.disabled=true;this.textContent='Working…';post('/actions/${a.id}/approve')">Approve</button>
    <button class="ghost" onclick="this.disabled=true;post('/actions/${a.id}/dismiss')">Dismiss</button></div>` : ""}</div>`;
}

function markDocs(c) {
  const read = new Set(c.events.filter((e) => e.kind === "read").map((e) => (e.detail || {}).id));
  document.querySelectorAll(".docs li[data-doc]").forEach((li) => {
    li.classList.toggle("seen", read.has(li.dataset.doc));
    li.classList.toggle("cited", !!c.final && c.final.quote_doc === li.dataset.doc);
  });
}

async function refresh() {
  const r = await fetch(`/api/case/${window.CASE_ID}`);
  if (!r.ok) return;
  const c = await r.json();
  $("#invoice").innerHTML = invoiceHtml(c.invoice);
  $("#invoice").classList.remove("muted");
  $("#state").textContent = STATE[c.state] || words(c.state);
  $("#state").className = "pill st-" + c.state;
  const key = JSON.stringify([c.events.length, c.questions, c.actions, c.state]);
  if (key !== lastKey) {
    lastKey = key;
    const t0 = c.events.length ? c.events[0].at : 0;
    $("#timeline").innerHTML = timelineHtml(c, t0) ||
      `<li class="empty muted">Not started. Press “Find out why”.</li>`;
    seen = new Set(c.events.map((e) => e.id));
    $("#result").innerHTML = resultHtml(c);
    $("#actions").innerHTML = c.actions.length ? `<div class="actions-h"><h3>Proposed actions</h3><div class="small muted">Nothing happens until you approve.</div></div>` + c.actions.map(actionHtml).join("") : "";
    markDocs(c);
    // On narrow screens the answer sits below the timeline; bring it into view once when it arrives.
    if (hadFinal === false && c.final && matchMedia("(max-width: 1100px)").matches) $("#result").scrollIntoView({behavior: "smooth", block: "start"});
    hadFinal = !!c.final;
  }
  $("#go").disabled = ["running", "needs_approval", "waiting_reply"].includes(c.state) || (["unbilled", "scope"].includes(c.mode) && !["new", "error"].includes(c.state));
  const checkLabel = c.state === "done" ? "Check complete" : "Review below";
  $("#go").textContent = c.mode === "unbilled" ? (c.state === "new" ? "Check for unbilled work" : c.state === "running" ? "Checking…" : c.state === "error" ? "Retry check" : checkLabel) : c.state === "new" ? "Find out why" : c.state === "running" ? "Investigating…" : "Run again";
  if (c.mode === "scope") $("#go").textContent = c.state === "new" ? "Check new requests" : c.state === "running" ? "Checking…" : c.state === "error" ? "Retry check" : checkLabel;
  clearTimeout(timer);
  timer = setTimeout(refresh, ["running", "waiting_reply"].includes(c.state) ? 1200 : 4000);
  chatState(c);
}

function chatState(c) {
  const approved = c.questions.some((q) => q.state === "ready_to_send");
  const open = ["done", "needs_approval", "waiting_owner"].includes(c.state) && !approved;
  $("#chat-text").disabled = $("#chat-send").disabled = !open;
  $("#chat-hint").textContent = approved ? "Chat opens again when the reply arrives." : !open ? (CHAT_HINT[c.state] || "")
    : c.state === "needs_approval" ? "Sending a message withdraws the unsent question. To send it, use its button."
    : "Chat never approves anything. Use the buttons to send or run.";
}

$("#chat").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const text = $("#chat-text").value.trim();
  if (!text) return;
  $("#chat-send").disabled = true;
  const r = await fetch(`/case/${window.CASE_ID}/chat`, {method: "POST", body: new URLSearchParams({message: text})});
  if (r.ok) $("#chat-text").value = "";
  else alert((await r.json().catch(() => ({}))).error || "Request failed");
  refresh();
});
$("#go").addEventListener("click", () => post(`/case/${window.CASE_ID}/investigate`));
refresh();
