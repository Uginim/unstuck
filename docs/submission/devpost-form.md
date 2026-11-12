# Devpost form: paste sheet

Fill the existing draft (currently named "RefundPilot"). Field order follows the form as read on 2026-11-12.

## 1. Project overview

**Project name**
```
Unstuck
```

**Elevator pitch** (200 characters max; this one is 180)
```
An AI agent that finds out why a freelancer's PayPal invoice is unpaid, asks the right person only after you approve, and proposes PayPal fixes that run only when you approve them.
```

## 2. Project details

**About the project**: paste `docs/submission/devpost.md` from "## Inspiration" through "## What's next" (Markdown is supported).

**Built with** (one tag at a time)
```
python
fastapi
sqlite
javascript
html
css
paypal
paypal-invoicing-api
claude
anthropic
httpx
jinja2
uvicorn
fly.io
```

**Try it out links**
```
https://unstuck-demo.fly.dev
https://github.com/Uginim/unstuck
```

**Image gallery**: upload from `docs/submission/images/` (screenshots taken from the recorded demo).

**Video demo link**: the YouTube URL after upload (public).

## 3. Additional info

| Field | Answer |
|---|---|
| Submitting as | Individual |
| Country of residence | Korea, Republic of |
| Organization / team | N/A |
| New or existing project | New |
| Sponsor tools used | None (the demo is hosted on Fly.io, which is not a listed sponsor) |
| Code repository URL | https://github.com/Uginim/unstuck |
| Demo URL | https://unstuck-demo.fly.dev |
| PayPal integration rating, feedback, contact OK | Your call |
| Eligibility checkboxes (adult, eligible country, not a sponsor employee) | You confirm and tick |
| Learning level | Your call |

**How PayPal and AI are used**
```
PayPal: the app uses the PayPal Invoicing v2 API in Sandbox. It reads an invoice's status, paid and due amounts; searches a client's invoices before proposing a new one (to avoid billing twice); and, only after the freelancer presses Approve, sends a reminder, enables partial payment with a minimum amount, or creates and sends an invoice for agreed, delivered work. After each write the app reads the invoice again and shows the result. All demo workspaces use one Sandbox merchant; no real money moves.

AI: Claude (claude-haiku-5-5 through the Anthropic Messages API) runs a tool-use loop. It decides which workspace documents to read, reads the live PayPal invoice, and can pause to propose one question to a named client contact. Code gates its conclusions: a quoted sentence must match a stored source, action fields are validated, and chat messages can never approve anything. A home chat uses the same model to start existing checks from plain-language requests.
```

**Testing instructions for judges**
```
Open https://unstuck-demo.fly.dev (the first visit after idle can take a few seconds to wake). Create an account with any email and a throwaway password of 8+ characters; no mail is sent. Setup creates five PayPal Sandbox invoices for fictional clients (about 30 seconds).

Suggested path: in Ask Unstuck type "Why hasn't Harbor & Vine paid?" and open the check. When it asks to approve a question, press Approve question, then I sent it, then open Simulated client inbox and press Submit simulated reply. Return to the case to see the confirmed cause and proposed actions. On Signal House Podcast, press Check for unbilled work and approve the proposed invoice to see a new Sandbox invoice created and sent.

Limits: 3 new accounts per network per day; a shared daily AI budget after which existing results stay readable. Accounts expire after 45 days.
```

**AI tools used during development**
```
Claude Code (Claude Opus) for planning, task briefs, code review and live browser testing; OpenAI Codex sessions for implementing and testing scoped tasks. All changes were reviewed and tested before commit.
```
