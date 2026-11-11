"""Scope evidence gate and source-bound change drafts; no financial actions."""
import re

from .unbilled import exact_sentence, name_words, validate_create

SCOPE_SCHEMA = {
    'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
        'required': ['classification', 'name', 'contract_doc', 'contract_quote', 'request_doc',
                     'request_quote', 'reason', 'price_doc', 'price_quote', 'timing_doc', 'timing_quote'],
        'properties': {
            'classification': {'type': 'string', 'enum': ['included', 'paid_change', 'unclear']},
            **{key: {'type': 'string'} for key in ('name', 'contract_doc', 'contract_quote', 'request_doc',
                'request_quote', 'reason', 'price_doc', 'price_quote', 'timing_doc', 'timing_quote')},
            'amount_usd': {'type': 'number'},
        }},
}


def evidence(docs, finding, prefix, kinds):
    doc = docs.get(finding.get(prefix + '_doc'))
    quote = finding.get(prefix + '_quote')
    if not doc or doc.get('kind') not in (*kinds, 'owner') or not exact_sentence(doc['text'], quote):
        raise ValueError(f'{prefix} needs a whole exact sentence from an appropriate source.')
    return quote


def validate_scope(answer, docs):
    findings = answer.get('scope_findings')
    if not isinstance(findings, list) or not findings:
        raise ValueError('Scope checks need request-level scope_findings.')
    if answer.get('proposed_actions'):
        raise ValueError('Scope checks accept no model actions; code prepares grounded drafts only.')
    seen = set()
    for f in findings:
        if not isinstance(f, dict) or f.get('classification') not in ('included', 'paid_change', 'unclear'):
            raise ValueError('Unknown scope classification.')
        if any(not isinstance(f.get(k), str) for k in SCOPE_SCHEMA['items']['required']):
            raise ValueError('Scope fields must be strings.')
        if not name_words(f['name']) or not f['reason'].strip():
            raise ValueError('Each request needs a name and explanation.')
        request = evidence(docs, f, 'request', ('mail', 'reply'))
        key = (f['request_doc'], request)
        if key in seen:
            raise ValueError('Duplicate request in scope check.')
        seen.add(key)
        if not name_words(f['name']) <= name_words(request):
            raise ValueError('Request name must come from the client request.')
        if f['classification'] != 'unclear':
            contract = evidence(docs, f, 'contract', ('contract',))
            # shortcut: conservative English negation check; ambiguous wording stays owner-reviewed.
            if f['classification'] == 'included' and re.search(
                    r"\b(not|never|no|outside|excluded|unless|except)\b|n't", contract, re.I):
                raise ValueError('Negative or conditional inclusion needs clarification, not an included conclusion.')
        if f['classification'] != 'paid_change' and (f.get('amount_usd') is not None or f['price_quote']):
            raise ValueError('Only a paid change may carry a price.')
        if f['classification'] == 'paid_change' and f['price_quote']:
            price = evidence(docs, f, 'price', ('contract', 'rate_card', 'reply'))
            if re.search(r"n't|\b(no|without|waived|cancelled|canceled)\b", price, re.I):
                raise ValueError('Negative price wording needs owner clarification.')
            price_docs = {k: {**v, 'kind': 'contract'} if v['kind'] == 'rate_card' else v
                          for k, v in docs.items()}
            validate_create({'currency': 'USD', 'recipient': 'scope', 'note': 'Draft only', 'items': [{
                'name': f['name'], 'amount_usd': f.get('amount_usd'),
                'quote': f['price_quote'], 'quote_doc': f['price_doc']}]}, price_docs, 'scope')
        elif f.get('amount_usd') is not None:
            raise ValueError('A price needs its exact source quote.')
        if f['timing_quote']:
            evidence(docs, f, 'timing', ('contract', 'rate_card', 'reply'))
        if f['classification'] != 'included' and answer['status'] != 'NEEDS_OWNER':
            raise ValueError('Paid or unclear scope requires NEEDS_OWNER; ask if facts are missing.')
    return findings


def change_drafts(answer, docs, recipient):
    findings = validate_scope(answer, docs)
    return [{'type': 'draft_reply', 'to': recipient, 'subject': 'Proposed change agreement',
             'why': 'Review scope, quoted price and timing before requesting written agreement.',
             'body': (f"Proposed additional scope: {f['name']}\n"
                      f"Your request: {f['request_quote']}\n"
                      f"Contract: {f['contract_quote']}\n"
                      f"Proposed price: USD {f['amount_usd']:.2f}\n"
                      f"Price basis: {f['price_quote']}\n"
                      f"Timing: {f['timing_quote'] or 'Not agreed; please confirm timing before work starts.'}\n\n"
                      "Please confirm this change in writing before work starts. This is a proposal, not an invoice. "
                      "No payment will be requested by this check.")}
            for f in findings if f['classification'] == 'paid_change' and f['price_quote']]
