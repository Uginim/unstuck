# Unstuck

## Inspiration

For a freelancer, the money side of client work spans more than overdue invoices. A payment may be blocked by missing information, delivered extras may never have been billed, and a new request may need an agreement before work begins. We built Unstuck to bring those decisions into one review flow, with PayPal Invoicing as the system of record for invoices and balances.

## What it does

Unstuck starts with a weekly money brief: what needs your approval, where you are waiting on a client, what is unclear, and what PayPal reports as owed. It is a current snapshot of tracked work, with totals separated by currency, not a weekly revenue report.

The freelancer can investigate an unpaid invoice, check for agreed and delivered but unbilled work, or check whether a new request is included in the contract. Claude chooses what evidence to read and can propose a specific question to a named contact. The freelancer can redirect the investigation or ask why through case chat. Chat instructions never approve an action.

For questions and reply/change-agreement drafts, approval produces an Open in Gmail link and a default-mail fallback. The user sends from their own mail app, marks it sent, and pastes a reply back. **The app sends no correspondence and reads no mailbox.** The demo also provides a clearly labeled simulated client inbox.

Supported PayPal actions include reminders, enabling partial payments, and creating/sending an invoice for supported unbilled work. Each requires a separate approval. A scope check prepares an agreement draft, not a financial action. A proposed rate-card price does not mean the customer accepted it.

## How we built it

Python and FastAPI serve the interface. SQLite stores transcripts, questions, approvals and results. Claude uses the Anthropic Messages API with workspace search/read tools, PayPal invoice reads and a question tool that pauses for human review.

The same loop handles investigation, unbilled work and scope checks. Code checks quoted sentences against stored sources, validates action fields and restricts available operations. It does not prove that a quote supports the model's interpretation. A limited English lexical check also rejects selected unsupported completion and attachment claims in drafts/reminder notes. It can miss other phrasing and does not establish general factual correctness. The model can still be wrong.

The PayPal Invoicing v2 Sandbox adapter reads invoices, searches a client's invoices, sends approved reminders, updates partial-payment settings and creates/sends approved invoices. Financial execution rereads PayPal and checks the result. The brief reuses stored results and existing invoice reads without new model calls. Chat uses the same gates and approval controls.

## Challenges

We had to distinguish missing evidence from a confident answer. A supplier hold code is a lead, not its explanation. Similarly, a request, a rate-card price, customer acceptance and completed work are different facts. Scope checks stop at an agreement draft; invoicing belongs to a separate check for agreed, delivered work.

We also separated approval from email delivery. A compose link does not send a message, and a user's “I sent it” is a self-report. Chat saying “approve” does not replace an approval button. PayPal state can change during a conversation, and an uncertain remote write must be reviewed rather than assumed successful.

## Accomplishments

The implementation connects a money brief, evidence-based investigation, scope review, unbilled-work proposals, owner chat and explicit approvals around PayPal Sandbox invoices.

In the combined HTTP rehearsal, PayPal Sandbox rereads confirmed Quill's partial-payment setting and unchanged invoice amount, and a new USD 180 Signal invoice with SENT status. Repeated approval was refused. Cedar's scope check produced an agreement draft without financial writes, and the sweep made no financial writes.

This was not an uncorrected all-pass run. Harbor and Atlas drafts falsely implied some work had already been done; the owner corrected them before approval. Three implementation bugs were fixed and rechecked. A reminder request was accepted, but delivery could not be verified from PayPal's GET response. Signal used an isolated fictional recipient alias because the shared Sandbox already contained a previous extra invoice. These qualifications are part of the result, not evidence of autonomous correctness.

In a targeted follow-up, Harbor and Atlas produced first displayed drafts phrased as requests or next steps without owner chat correction. The new honesty guard did not reject a live draft in that run; its rejection/rewrite path was exercised in scripted tests. No reminder was proposed, so live reminder rewriting was not checked. This is a bounded improvement, not a guarantee that drafts are truthful or that the whole demo was rerun.

All clients and workspace documents are fictional and seeded. A simulated external-payment record is not a verified bank transfer. An invoice sent in Sandbox is not revenue recovered, and enabling partial payment is not payment collection. We have not measured customer time savings or business impact.

## What we learned

A useful next step may be a narrower question or a decision not to bill yet. Visible evidence and separate approvals make that decision inspectable, but they do not make the model infallible.

Run 6 of our earlier investigation experiment **failed its pre-registered scoring**. Later semantic re-grading was post-hoc, with one grader on ten synthetic cases. It does not prove superiority, cost savings at equal quality or an agent-only capability. The experiment harness and this app are distinct.

## What's next

Test usefulness with freelancers using consented data. Add authenticated, isolated workspaces before handling real customer information. Evaluate mailbox integration separately from today's compose-link workflow. Strengthen semantic validation, duplicate detection and recovery after uncertain writes, and repeat evaluation with fresh cases, preregistered scoring and independent graders.

## Built with

PayPal Sandbox Invoicing v2, Claude through the Anthropic Messages API, Python, FastAPI, Uvicorn, HTTPX, Jinja2, SQLite, HTML, CSS and JavaScript. Gmail compose links open the user's mail application; there is no Gmail API integration.
