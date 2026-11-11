"""Demo workspace for one fictional freelancer. Every name, company and address here is made up.

Each scenario seeds a real PayPal sandbox invoice plus the freelancer's mail, contract and work notes.
`contact_replies` are what the client-side person answers in the demo inbox; a human presses send.
"""
FREELANCER = {"name": "Mina Park", "email": "mina@minapark-studio.example.com", "trade": "independent brand designer"}

SCENARIOS = [
    {
        "key": "harbor",
        "client": "Harbor & Vine",
        "title": "Brand guidelines and label set",
        "items": [("Brand guidelines", 98000), ("Wine label set (4)", 50000)],
        "invoice_date": "2026-10-14", "terms_days": 15,
        "recipient": ("Jordan Lee", "jordan.lee@harborvine.example.com"),
        "seed_payment": None,
        "docs": [
            {"id": "hv-m1", "kind": "mail", "title": "Re: Invoice for brand guidelines",
             "text": "From: jordan.lee@harborvine.example.com\nDate: 2026-11-09\n\nHi Mina, finance says your invoice is "
                     "\"in the queue\" but nothing has gone out. Maybe the PayPal link expired? Could you resend it? "
                     "Sorry about this, the guidelines are already on our shelves!\nJordan"},
            {"id": "hv-m2", "kind": "mail", "title": "Supplier portal notification",
             "text": "From: no-reply@suppliers.harborvine.example.com\nDate: 2026-11-01\n\nSupplier record SUP-4471 "
                     "(Mina Park Studio) status changed: ON HOLD. Reason code: VND-HLD-07. Questions about supplier "
                     "records: ap@harborvine.example.com"},
            {"id": "hv-c1", "kind": "contract", "title": "Services agreement (excerpt)",
             "text": "Payment: within 15 days of the invoice date, via the invoice link provided by the designer. "
                     "Supplier keeps its tax and registration documents current with Harbor & Vine accounts payable."},
            {"id": "hv-w1", "kind": "work", "title": "Delivery log",
             "text": "2026-10-12 delivered final guidelines PDF and 4 label files. 2026-10-13 Jordan Lee approved in "
                     "writing: \"Approved, sending to print.\""},
        ],
        "contacts": {"ap@harborvine.example.com": "Accounts payable, Harbor & Vine",
                     "jordan.lee@harborvine.example.com": "Jordan Lee, marketing lead (client)"},
        "contact_replies": {
            "ap@harborvine.example.com":
                "Hi Mina, VND-HLD-07 means the W-9 tax form we have on file for you expired on Oct 31; we require "
                "a renewed one every year. Our weekly payment run skips suppliers on hold, so your invoice was "
                "skipped twice. The invoice and link are fine. Send a new W-9 to this address and we will release "
                "the hold before the Nov 14 run. - Dana, AP",
            "jordan.lee@harborvine.example.com":
                "I honestly don't know, finance just said it's in the queue. Maybe ask ap@harborvine.example.com?",
        },
        "truth": "Expired W-9 tax form put the supplier record on hold; payment runs skip it. Link is fine.",
    },
    {
        "key": "quill",
        "client": "Quill Labs",
        "title": "Product launch video (2 cuts)",
        "items": [("Launch video, first cut", 140000), ("Launch video, final cut", 140000)],
        "invoice_date": "2026-10-20", "terms_days": 15,
        "recipient": ("Priya Raman", "priya@quilllabs.example.com"),
        "seed_payment": None,
        "docs": [
            {"id": "ql-m1", "kind": "mail", "title": "Invoice rejected?",
             "text": "From: priya@quilllabs.example.com\nDate: 2026-11-08\n\nHey Mina, our system bounced your invoice "
                     "when I tried to pay. I think it might be the comma in 2,800.00? Could you cancel it and reissue "
                     "as 2800 so it goes through? Forwarding the error below.\nPriya"},
            {"id": "ql-m2", "kind": "mail", "title": "Fwd: Payment request blocked",
             "text": "From: workflow@quilllabs.example.com\nTo: priya@quilllabs.example.com\n\nPayment request for "
                     "\"Mina Park Studio\" USD 2,800.00 could not be completed. Approval route APR-2K: status "
                     "PENDING_L2. Policy questions: finance-ops@quilllabs.example.com"},
            {"id": "ql-c1", "kind": "contract", "title": "Statement of work",
             "text": "Fee USD 2,800 in two milestones of USD 1,400: first cut, final cut. Client contact and "
                     "approver: Priya Raman."},
            {"id": "ql-w1", "kind": "work", "title": "Delivery log",
             "text": "2026-10-09 first cut delivered, approved by Priya 10-10. 2026-10-19 final cut delivered, "
                     "approved by Priya 10-20."},
        ],
        "contacts": {"finance-ops@quilllabs.example.com": "Finance operations, Quill Labs",
                     "priya@quilllabs.example.com": "Priya Raman, product lead (client)"},
        "contact_replies": {
            "finance-ops@quilllabs.example.com":
                "Hi Mina, APR-2K is our approval limit: Priya can approve up to USD 2,000 per payment. Anything "
                "above needs CFO sign-off, which is taking about three weeks this quarter. The amount format is "
                "not the problem. If your invoice accepts partial payments, Priya can pay up to 2,000 now and the "
                "rest once the CFO signs. - Leo, Finance Ops",
            "priya@quilllabs.example.com":
                "Not sure! The error just says APR-2K. finance-ops@quilllabs.example.com would know.",
        },
        "truth": "Single-approver limit USD 2,000 (APR-2K); invoice is above it. Partial payments let her pay now.",
    },
    {
        "key": "atlas",
        "client": "Atlas Coffee Roasters",
        "title": "Menu redesign and social templates",
        "items": [("Menu redesign", 50000), ("Social templates (3)", 40000)],
        "invoice_date": "2026-10-16", "terms_days": 15,
        "recipient": ("Sam Ortiz", "sam@atlascoffee.example.com"),
        "seed_payment": {"cents": 50000, "date": "2026-11-03", "note": "Bank transfer from Atlas Coffee Roasters"},
        "docs": [
            {"id": "ac-m1", "kind": "mail", "title": "Already paid!",
             "text": "From: sam@atlascoffee.example.com\nCc: books@atlascoffee.example.com\nDate: 2026-11-10\n\nMina, we "
                     "paid this last week, please stop the reminders. Our bookkeeper sent it as PO-118.\nSam"},
            {"id": "ac-m2", "kind": "mail", "title": "Re: Add social templates?",
             "text": "From: sam@atlascoffee.example.com\nDate: 2026-10-05\n\nYes, go ahead with the 3 social templates "
                     "for an extra USD 400. Thanks!\nSam"},
            {"id": "ac-c1", "kind": "contract", "title": "Quote Q-31",
             "text": "Menu redesign, fixed fee USD 500. Changes in scope are agreed by email and added to the invoice."},
            {"id": "ac-w1", "kind": "work", "title": "Delivery log",
             "text": "2026-10-11 menu files delivered. 2026-10-15 three social templates delivered; Sam replied "
                     "\"love them\"."},
        ],
        "contacts": {"books@atlascoffee.example.com": "Bookkeeper, Atlas Coffee Roasters",
                     "sam@atlascoffee.example.com": "Sam Ortiz, owner (client)"},
        "contact_replies": {
            "books@atlascoffee.example.com":
                "Hi Mina, PO-118 was raised from your original quote Q-31 for USD 500, and that's what I transferred "
                "on Nov 3. I never saw an approval for the extra templates, so I didn't add them. If Sam approved it, "
                "send me the remaining amount and I'll pay it this week. - Ana",
            "sam@atlascoffee.example.com":
                "Ana (books@atlascoffee.example.com) handles all payments, she'd know the details.",
        },
        "truth": "Bookkeeper paid the original quote (USD 500) under PO-118; the approved +USD 400 change never reached her.",
    },
]


SCENARIOS.append({
    "key": "signal", "client": "Signal House Podcast", "title": "Podcast cover artwork",
    "items": [("Podcast cover artwork", 60000)], "invoice_date": "2026-11-04", "terms_days": 15,
    "recipient": ("Alex Chen", "alex@signalhouse.example.com"), "seed_payment": None,
    "docs": [
        {"id": "sh-m1", "kind": "mail", "title": "Re: Podcast artwork invoice",
         "text": "From: alex@signalhouse.example.com\n\nThe podcast cover artwork invoice is scheduled for its due date. Please check that the extra work we approved is also invoiced."},
        {"id": "sh-m2", "kind": "mail", "title": "Approved extra episode cards",
         "text": "From: alex@signalhouse.example.com\n\nYes, I approve three episode cards for an additional USD 180. Please deliver them with the cover artwork."},
        {"id": "sh-c1", "kind": "contract", "title": "Podcast artwork agreement",
         "text": "Podcast cover artwork costs USD 600. Extra episode cards are outside this original scope and require a separate written approval. Payment is due within 15 days of the invoice date."},
        {"id": "sh-w1", "kind": "work", "title": "Delivery and billing log",
         "text": "2026-11-03: Delivered and Alex accepted the podcast cover artwork and three episode cards. The billing log records only the cover artwork invoice; verify the current PayPal invoices before billing extras."},
    ],
    "contacts": {"alex@signalhouse.example.com": "Alex Chen, podcast producer"},
    "contact_replies": {"alex@signalhouse.example.com": "The cover invoice is not overdue and is scheduled for its due date. I approved and accepted the three episode cards for USD 180; please invoice those separately if they are not already covered."},
    "truth": "Three episode cards were approved for USD 180 and delivered, but only the USD 600 cover artwork was invoiced.",
})

SCENARIOS.append({
    "key": "cedar", "client": "Cedar Lantern Studio", "title": "Cover artwork and new requests",
    "items": [("Cover artwork", 60000)], "invoice_date": "2026-11-10", "terms_days": 15,
    "recipient": ("Riley Vale", "riley@cedarlantern.example.com"), "seed_payment": None,
    "docs": [
        {"id": "cl-m1", "kind": "mail", "title": "Two small artwork changes",
         "text": "From: jo@cedarlantern.example.com\n\nPlease revise the cover title color in our second revision round. Please create three episode cards using the cover title color. Riley decides scope and pricing; I only coordinate artwork."},
        {"id": "cl-c1", "kind": "contract", "title": "Signed cover artwork agreement",
         "text": "Cover title color changes are included in two revision rounds. Three episode cards are outside the cover artwork scope and require a separate written agreement before work starts. Riley Vale is the client approver for scope and pricing."},
        {"id": "cl-r1", "kind": "rate_card", "title": "Mina's current rate card",
         "text": "Three episode cards cost USD 180. Delivery of three episode cards takes five business days after written change approval."},
        {"id": "cl-w1", "kind": "work", "title": "Revision and delivery log",
         "text": "The cover artwork was delivered and one revision round was used. The second revision round is available. Episode cards have not been started or approved."},
    ],
    "contacts": {"jo@cedarlantern.example.com": "Jo Lark, artwork coordinator",
                 "riley@cedarlantern.example.com": "Riley Vale, scope and price approver"},
    "contact_replies": {
        "jo@cedarlantern.example.com": "Riley decides scope and pricing; please ask Riley about episode cards.",
        "riley@cedarlantern.example.com": "The cover title color change uses our remaining included revision. Three episode cards cost USD 180. Please send the proposed change agreement for review before starting. Delivery of three episode cards takes five business days after written change approval.",
    },
    "truth": "Cover color revision is included; episode cards need a separate proposed USD 180 agreement, not an invoice.",
})
