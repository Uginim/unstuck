"""Read-only money brief and compose links. This module never sends email."""
from datetime import datetime, timezone
import re
from urllib.parse import urlencode, quote


def compose(to, subject, body):
    # One stored recipient only: reject header injection and multi-address payloads.
    if not isinstance(to, str) or not re.fullmatch(r"[^\s@,;<>?&#]+@[^\s@,;<>?&#]+", to):
        return None
    if not isinstance(subject, str) or any(c in subject for c in '\r\n') or not isinstance(body, str):
        return None
    return {
        'gmail': 'https://mail.google.com/mail/?' + urlencode(
            {'view': 'cm', 'fs': '1', 'to': to, 'su': subject, 'body': body}, quote_via=quote),
        'mailto': 'mailto:' + quote(to, safe='@') + '?' + urlencode(
            {'subject': subject, 'body': body}, quote_via=quote),
    }


def question_links(q):
    return compose(q['to_addr'], 'Question about your invoice', q['question']) if q['state'] == 'ready_to_send' else None


def action_links(a):
    p = a['params']
    return compose(p.get('to'), p.get('subject') or 'Regarding your invoice', p.get('body')) if (
        a['type'] == 'draft_reply' and a['state'] in ('ready_to_send', 'done')) else None


CHECK_LABELS = {'investigate': 'Why unpaid', 'unbilled': 'Unbilled work', 'scope': 'New requests'}


def money_brief(store, rows, workspace=None):
    groups = {'Needs your approval': [], 'Waiting on client': [], "Don't bill yet / unclear": []}
    with store.db() as db:
        if workspace is None:
            ids = [r[0] for r in db.execute('SELECT id FROM cases ORDER BY created')]
        else:  # one visitor's workspace only ('' in local mode)
            ids = [r[0] for r in db.execute('SELECT id FROM cases WHERE workspace=? ORDER BY created', (workspace,))]
    for cid in ids:
        c = store.case(cid)
        def add(group, reason):
            reason = re.split(r'(?<=[.!?])\s+', ' '.join(reason.split()))[0]
            groups[group].append({'id': cid, 'client': c['client'], 'mode': c.get('mode') or 'investigate',
                                  'reason': reason[:200] + ('…' if len(reason) > 200 else '')})
        for q in store.questions(cid):
            if q['state'] in ('needs_approval', 'ready_to_send'):
                add('Needs your approval', ('Ready to send: ' if q['state'] == 'ready_to_send' else 'Review question: ') + q['question'])
            elif q['state'] == 'sent':
                add('Waiting on client', 'You marked as sent: ' + q['question'])
        for a in store.actions(cid):
            if a['state'] in ('proposed', 'ready_to_send'):
                add('Needs your approval', ('Ready to send: ' if a['state'] == 'ready_to_send' else 'Review action: ') + (a['params'].get('why') or a['type'].replace('_', ' ')))
        f = c['final'] or {}
        if f.get('status') in ('NEEDS_OWNER', 'EXHAUSTED'):
            add("Don't bill yet / unclear", f.get('summary_for_owner') or f.get('cause') or 'Review the stored result.')
    # One client can have lines from its investigation and its checks; name the check only then.
    for lines in groups.values():
        for line in lines:
            modes = {x['mode'] for x in lines if x['client'] == line['client']}
            line['check'] = CHECK_LABELS.get(line['mode'], line['mode']) if len(modes) > 1 else ''
    totals, owed, seen = {}, [], set()
    unavailable = 0
    for r in rows:
        if r['invoice_id'] in seen:
            continue
        seen.add(r['invoice_id'])
        i = r['live']
        if not i or i.get('due_cents') is None or not i.get('currency'):
            unavailable += 1
            continue
        if i['status'] in ('CANCELLED', 'REFUNDED') or i['due_cents'] <= 0:
            continue
        currency = i['currency']
        totals[currency] = totals.get(currency, 0) + i['due_cents']
        owed.append({'id': r['id'], 'client': r['client'], 'currency': currency,
                     'cents': i['due_cents'], 'reason': (r.get('final') or {}).get('cause') or 'PayPal reports an outstanding balance.'})
    return {'groups': groups, 'totals': totals, 'owed': owed, 'unavailable': unavailable,
            'read_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
