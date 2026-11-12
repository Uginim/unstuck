# Unstuck

Unstuck helps a freelancer manage the money side of client work: what's stuck, what's unbilled, what's out of scope, and what needs attention this week. PayPal Invoicing is the system of record for invoice status and outstanding balances. Claude reads the available workspace evidence and proposes questions or next steps for the freelancer to review.

**Live demo:** https://unstuck-demo.fly.dev (create an account with any email; no mail is sent). The first visit after idle can take a few seconds to wake.

**Demo video (2:08):** https://youtu.be/z6PSSjBNtcw

**This is a sandbox prototype.** Clients, emails, contracts, rate cards and work records are fictional, seeded text. PayPal operations use Sandbox. The app does not read a real mailbox or send correspondence: it prepares Gmail/default-mail compose links, the user sends from their own email app, and replies are pasted back. The simulated client inbox is an alternative for the demo. No recovered revenue or real customer outcome is claimed.

## What you can do

| Task | Workflow | Boundary |
|---|---|---|
| Ask in plain words | **Ask Unstuck** on the home page starts one of the checks below ("Why hasn't Quill paid?", "Did I forget to bill anyone?") or answers from the current brief. | It can only start the four existing checks. It cannot approve, send or change an invoice; those stay buttons on the case page. |
| Decide what to do this week | The money brief groups approval requests, client replies awaited, unresolved decisions and balances owed by currency. | A current snapshot of tracked cases, not seven-day accounting or a cash forecast. It makes no extra model calls. |
| Find why an invoice is unpaid | **Find out why** searches documents, reads PayPal and can ask a named contact about missing evidence. | A conclusion can be wrong even when its quote exists. |
| Find agreed work not yet billed | **Check for unbilled work** compares approval/delivery records with the client's PayPal invoices. | Only evidenced work can become a proposed new invoice; creation and sending need a separate approval. |
| Check a new client request | **Check new requests** distinguishes an included revision, paid change or unclear request and prepares a change-agreement draft where supported. | A rate-card price is a proposal, not client acceptance. This check cannot execute PayPal financial actions. |
| Direct the investigation | **Tell Unstuck** accepts instructions or a follow-up question on an eligible case. | Chat is never approval. “Yes, send it” does not send mail or execute a PayPal action. |
| Review all tracked invoices | **Check all invoices** runs eligible new investigations and unbilled checks. | It queues proposals, does not approve actions, and does not import every invoice in the account as a case. |

## How it works

1. In the hosted demo, create an account (any email, a throwaway password). Then start from **Ask Unstuck**, the brief or an invoice card. Choose an investigation, unbilled check or scope check.
2. Claude, through the Anthropic Messages API, chooses searches, document reads, PayPal reads and questions. The timeline records those steps. The home chat makes one Messages API call per message with only the four start tools available; the checks it starts run the same loop as the buttons.
3. A question pauses the loop. **Approve question** prepares a fixed-recipient draft. **Open in Gmail** or the default-mail link opens it in the user's mail app. The user sends there, then selects **I sent it**. The app says **Ready to send** or **You marked as sent**, not delivery confirmed.
4. Paste the client's reply on the case page. For the fictional walkthrough, use **Simulated client inbox (demo)** instead. The submitted text becomes evidence and resumes the same investigation.
5. Review the result, quotes and proposed actions. Each action has separate approval controls. Approving an email draft prepares a compose link; approving a PayPal action invokes the corresponding Sandbox operation.

The demo's `.example.com` addresses are fictional. Do not send to them or substitute real customer information in the public demo. A simulated mark-sent/reply exercise is not evidence that email was delivered.

Chat can redirect an unapproved question; that question is withdrawn and any replacement needs a new approval. Chat is blocked while a question is already approved for sending or waiting for its reply. Owner messages are not source documents for quote/price checks. A revised conclusion can replace proposals that have not been acted on.

### Evidence and stopping

| End state | Meaning in the relevant task |
|---|---|
| `CONFIRMED` | The model reports a supported finding. |
| `NEEDS_OWNER` | A decision or unresolved agreement needs the freelancer. |
| `EXHAUSTED` | The question budget is used and uncertainty remains. |
| `NOT_A_BLOCKER` | No payment blocker, or no further billing action in the check. |

The prompt asks the model to investigate missing evidence rather than finish on a guess. Each final answer passes a code gate. Quote checks establish that whole sentences exist in the cited documents after whitespace normalization. They **do not prove the conclusion follows from those sentences**, that a contract applies, or that a reply is true. Additional action checks validate required fields, prices and allowed operations. A narrow English lexical guard checks draft subjects/bodies, change-agreement drafts and reminder notes for selected unsupported completion claims, and rejects attachment claims because the app does not attach files. It uses recorded evidence and permits exact attributed source quotations. It is not a general fact checker: other wording/languages can escape it and honest paraphrases can be rejected. The model can still misinterpret evidence.

The loop limits counted questions to two; questions withdrawn through chat do not count. It also has a model-step budget, daily spend checks and request limits. SQLite preserves transcripts, questions, results and actions, but an interrupted in-flight operation may require manual review.

### PayPal Invoicing and approvals

| Purpose | Sandbox API call |
|---|---|
| Access token | `POST /v1/oauth2/token` |
| Read current invoice | `GET /v2/invoicing/invoices/{id}` |
| Check client's existing invoices | `POST /v2/invoicing/search-invoices`, then invoice GETs |
| Approved reminder | `POST /v2/invoicing/invoices/{id}/remind` |
| Approved partial-payment setting | `PUT /v2/invoicing/invoices/{id}`, then GET |
| Approved agreed-work invoice, also demo seeding | `POST /v2/invoicing/invoices`, then `POST /v2/invoicing/invoices/{id}/send`, then GET |
| Atlas fixture setup only | `POST /v2/invoicing/invoices/{id}/payments` records a fictional external payment |

The agent proposes; application code executes approved financial actions after fresh PayPal reads. Partial-payment updates compare selected invoice fields afterward. New-invoice approval rechecks evidence and existing invoices, uses stable request IDs, and checks the returned invoice. Scope checks cannot perform financial actions. Existing invoice cancellation, reissue, amount changes and payment recording are not agent actions.

These controls do not make remote writes atomic or guarantee exactly-once execution under every crash or timeout. Inspect an ambiguous result before retrying. Enabling partial payment does not collect a payment. Creating/sending a Sandbox invoice does not demonstrate revenue. Atlas's external-payment record is fixture data, not a verified bank transfer.

## Run locally

Use Python 3.12 or 3.13 and an installed `uv`. From the repository root:

```sh
uv sync --locked
# Only if .env does not already exist:
cp .env.example .env
```

Fill the three empty entries locally with your PayPal **Sandbox** client ID/secret and Anthropic API key. Never overwrite existing secrets or use production payment credentials. The app does not load `.env` itself. With shell-compatible assignments in that file:

```sh
set -a
. ./.env
set +a
UNSTUCK_AUTOCREATE=0 .venv/bin/python -m uvicorn unstuck.app:create_app --factory --host 127.0.0.1 --port 8101
```

Open `http://127.0.0.1:8101/`. A blank database seeds the five fictional scenarios as Sandbox invoices, including Atlas's synthetic external-payment record. Startup therefore makes Sandbox writes. Investigations and chat incur model usage; page refreshes can read PayPal.

To try the hosted flow locally, add `UNSTUCK_MODE=public` and a random `UNSTUCK_SESSION_SECRET` of at least 32 characters. The start page then shows **Create account / Log in**, and each new account seeds its own five Sandbox invoices.

| Optional variable | Default | Purpose |
|---|---|---|
| `UNSTUCK_MODEL` | `claude-haiku-5-5` | Anthropic model identifier; account availability may vary. |
| `UNSTUCK_DAILY_USD` | `3` | Application-estimated daily model budget, not a provider billing guarantee. |
| `UNSTUCK_DB` | `logs/unstuck.db` | SQLite state. |
| `UNSTUCK_LOG` | `logs/unstuck.jsonl` | Request/response log; keep private. |
| `UNSTUCK_SWEEP_HOURS` | `0` | Optional sweep interval; zero disables periodic sweeps. |
| `UNSTUCK_AUTOCREATE` | `1` | Set to zero with the factory command to avoid a second app at import. |
| `UNSTUCK_MODE` | `local` | `local`: one shared workspace, seeded at startup, no login. `public` (Docker default): visitors **Create account / Log in**; each account owns a private workspace with its own Sandbox invoices. |
| `UNSTUCK_SESSION_SECRET` | none | Required in `public` mode, at least 32 characters; signs the HttpOnly workspace cookie. Changing it logs everyone out. |
| `UNSTUCK_WORKSPACES_PER_IP` / `_PER_DAY` | `3` / `60` | New public accounts (workspaces) per network and in total per rolling 24 hours. Each one creates five Sandbox invoices. |
| `UNSTUCK_ACCOUNT_DAYS` | `45` | Account and workspace lifetime from signup. Logging in does not extend it. Expired accounts are deleted from the DB; their Sandbox invoices stay. |
| `UNSTUCK_WORKSPACE_HOURS` | `48` | Lifetime of older cookie-only workspaces without an account. |

Do not use empty numeric settings. Create a custom DB's parent directory first. Logs can contain full documents, model exchanges and payment responses. Keep `.env`, logs and databases out of Git and screenshots.

## Fictional walkthrough

| Client | Evidence to inspect | Intended next step, not a guaranteed model output |
|---|---|---|
| Harbor & Vine | A resend request and supplier hold code. The AP demo reply explains a client-specific annual tax-document refresh policy. | Prepare the requested-document response; the app does not upload a tax form or release the hold. This is not general W-9 expiry guidance. |
| Quill Labs | A USD 2,800 invoice and a finance reply explicitly permitting partial payments while a further approval is pending. | Review a partial-payment proposal without cancelling/reissuing the invoice. No payment is collected by changing the setting. |
| Atlas Coffee Roasters | USD 900 total, a synthetic USD 500 external-payment record, and approval for USD 400 extra work. | Explain the balance and review a reply or reminder; do not invent another payment. |
| Signal House Podcast | USD 600 cover artwork plus separately approved and delivered USD 180 episode cards. | Check all that client's invoices before proposing a separate invoice for the extras. |
| Cedar Lantern | A covered color revision and a new episode-card request, with contract and rate-card evidence. | Separate included work from a proposed paid change. Seek written agreement before work; do not invoice the request yet. |

For a simulated question journey: approve the question, exercise **I sent it** as a clearly labeled demo step, submit the fictional reply in `/inbox`, and return to the result. For real correspondence outside the synthetic demo, the user must actually send in their mail app and paste the reply; this prototype does not connect to their mailbox.

Public **Reset demo is disabled**. In `public` mode a visitor's actions only touch their own workspace's invoices; another visitor's case, question and action ids answer 404. Do not delete a DB to retry an uncertain financial write. Completed scope/unbilled checks are reused rather than repeatedly creating proposals.

## Verification and limits

The 2026-11-11 combined HTTP rehearsal against PayPal Sandbox confirmed the brief, question approval and pasted-reply flow, Quill's partial-payment setting, Signal's new SENT invoice and duplicate-approval refusal, Cedar's scope draft, owner chat and a sweep with no financial writes. The suite reported then was **420 passed, 19 subtests passed**, with JavaScript syntax checks passing.

**It was not an uncorrected all-pass demonstration.** Harbor and Atlas drafts claimed work had been done when it had not; the owner corrected them in chat before approval. Three implementation bugs were fixed and rechecked. Atlas's reminder POST was accepted, but PayPal's GET supplied no reminder history, so delivery was not independently verified. No correspondence was sent from Gmail and no payment was collected.

Signal used an isolated synthetic recipient alias for this rehearsal because an earlier extra invoice already existed in the shared Sandbox. The duplicate check was not bypassed or the old invoice deleted. This does not establish a clean rerun with the original recipient. The rehearsal used HTTP rather than browser interaction. Stored DB/JSONL data supported reconstructing one case, but did not establish a complete audit proof.

A follow-up T11 HTTP run checked Harbor and Atlas again without owner chat corrections. Their first displayed drafts used requests/next steps rather than unsupported completion claims. The latest reported suite was **459 passed, 19 subtests passed**, and JavaScript syntax passed. The live run did **not** trigger the new honesty guard: rejection and rewrite behavior were checked in scripted regression tests. The existing quote gate did reject and retry an initial final-answer candidate in each case. No reminder was proposed in that follow-up, so live reminder rewriting remains unverified. Existing stored drafts were not retroactively cleaned, and the guard does not cover all chat factuality. Review before approval remains necessary. This targeted follow-up does not replace the broader T9 qualifications above.

The exploratory investigation experiment is documented in [the results](experiments/f2_blockers/RESULTS.md). **Run 6 failed its pre-registered scoring.** Later semantic re-grading was post-hoc, with one grader on ten synthetic cases. It does not establish superiority, production accuracy, equal-quality cost savings or a capability available only to agents. That harness is not the deployed application.

Later real-browser (Playwright, Chromium) checks covered the merged interface at desktop, phone and dark-mode sizes, the home chat and demo accounts, including logging out and back in to the same work and cross-account 404s. They did not re-observe Signal invoice creation with per-account recipient aliases. The latest local suite on 2026-11-12 is **517 passed, 19 subtests passed** (`.venv/bin/python -m pytest -q`), and `node --check unstuck/static/app.js` passes.

This is a synthetic-data prototype. In `public` mode, demo accounts are an email identifier and a password: the email is never verified or written to, and there is no password recovery. Each account's workspace is private, but every workspace uses the same PayPal Sandbox merchant configured for the app; users do not connect their own PayPal accounts. The daily model budget is shared by all visitors. It has no mailbox ingestion, automatic email delivery or reply detection. New-billing/scope amount handling is USD-oriented; separate currency totals in the brief are not general multi-currency accounting. Duplicate invoice checks are lexical and can miss or overblock matches. An exact quote is not a guarantee of agreement, delivery or correct interpretation.

The deployment assumes one process/instance. A hosted version needs persistent state, controlled secrets, budget monitoring and an owner responsible for availability. A compose URL includes the draft text and does not verify delivery. Do not use public demo forms for confidential client data.

## Deploy on Fly.io (the hosted demo)

[`fly.toml`](fly.toml) runs one Docker machine (shared CPU, 256 MB) built from the [`Dockerfile`](Dockerfile), with a 1 GB volume mounted at `/app/data` for the SQLite database, log and daily spend record. The machine stops when idle and starts on the next request.

1. `fly apps create unstuck-demo` and `fly volumes create unstuck_data --app unstuck-demo --region nrt --size 1`.
2. Set secrets without printing them: `fly secrets set --app unstuck-demo PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... ANTHROPIC_API_KEY=... UNSTUCK_SESSION_SECRET=...` (PayPal **Sandbox** keys; keep the session secret stable across deploys).
3. `fly deploy --ha=false`, then check that `/health` returns 200 and the home page shows **Create account / Log in**.

Keep one machine and one worker: the visitor limits are process-local.

## Deploy on Render (alternative)

[`render.yaml`](render.yaml) is a Render Blueprint for one Docker web service (`unstuck-demo`, Starter plan, a single instance) built from the [`Dockerfile`](Dockerfile).

1. In Render, choose **New → Blueprint**, connect this repository, and select `render.yaml`.
2. Enter the three secrets Render asks for: your PayPal **Sandbox** `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`, and `ANTHROPIC_API_KEY`. `UNSTUCK_SESSION_SECRET` is generated by the Blueprint; keep it stable across deploys.
3. Keep the 1 GB persistent disk mounted at `/app/data`. The SQLite database, the log and the daily spend record live there; without it a redeploy loses every account, case and the day's spend record.
4. Deploy, then check that `/health` returns 200 and that the home page shows **Create account / Log in**.

The Blueprint sets `UNSTUCK_MODE=public`, the visitor limits above and `UNSTUCK_DAILY_USD=3`. Auto-deploy is off; deploy manually. The limits are process-local, so keep one instance and one worker. Render's monthly charges and model usage are separate from this app's spend estimate.

## Code map

- `unstuck/app.py`, `unstuck/store.py`: routes, approvals, SQLite state, sweep and execution.
- `unstuck/agent.py`, `unstuck/prompt.md`: shared investigation loop and gates.
- `unstuck/unbilled.py`, `unstuck/scope.py`, `unstuck/honesty.py`: evidence checks, change-agreement construction and limited draft-claim checks.
- `unstuck/brief_email.py`, `unstuck/chat.py`, `unstuck/home_chat.py`: brief, compose links, case chat and the home Ask Unstuck chat.
- `unstuck/workspaces.py`, `unstuck/accounts.py`: public-mode workspaces, signed cookies, demo accounts and limits.
- `unstuck/invoicing.py`: PayPal Invoicing adapter.
- `unstuck/scenarios.py`: fictional workspace and invoice fixtures.
- `unstuck/templates/`, `unstuck/static/`: interface.
- `daybreak/paypal.py`, `daybreak/logbook.py`: shared Sandbox transport and logging.

## License

MIT. See [LICENSE](LICENSE). Copyright (c) 2026 Hyeonuk Kim.
