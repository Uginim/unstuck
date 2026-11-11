"""Deterministic evidence and duplicate checks shared by proposal and approval."""
import hashlib
import re
from decimal import Decimal
from difflib import SequenceMatcher


def exact_sentence(text, quote):
    if not isinstance(quote, str) or len(quote.strip()) < 8:
        return False
    norm = lambda s: re.sub(r'\s+', ' ', s).strip()
    return norm(quote) in {norm(s) for s in re.split(r'(?<=[.!?])\s+|\n\s*\n', text)}


def cents(amount):
    if type(amount) not in (int, float):
        raise ValueError('Amount must be a positive USD number in whole cents.')
    value = Decimal(str(amount))
    if not value.is_finite() or value <= 0 or value * 100 != (value * 100).to_integral_value():
        raise ValueError('Amount must be a positive USD number in whole cents.')
    return int(value * 100)


def name_words(name):
    return {w.rstrip('s') for w in re.findall(r'[a-z]+', name.lower())
            if w not in {'a', 'the', 'and', 'of', 'for', 'additional', 'extra'}}


def similar_name(left, right):
    a, b = name_words(left), name_words(right)
    if not a or not b:
        return False
    return (a <= b or b <= a or len(a & b) / len(a | b) >= .5
            or SequenceMatcher(None, ' '.join(sorted(a)), ' '.join(sorted(b))).ratio() >= .8)


def validate_create(action, docs, recipient):
    if action.get('currency') != 'USD' or action.get('recipient') != recipient:
        raise ValueError('New invoices must use USD and this client’s recipient email.')
    if not isinstance(action.get('note'), str) or not action['note'].strip():
        raise ValueError('New invoices need a note.')
    items = action.get('items')
    if not isinstance(items, list) or not items:
        raise ValueError('New invoices need agreed items.')
    checked = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not name_words(item['name']):
            raise ValueError('Each item needs a meaningful name.')
        amount = cents(item.get('amount_usd'))
        doc = docs.get(item.get('quote_doc'))
        quote = item.get('quote')
        if not doc or doc.get('kind') not in ('mail', 'contract', 'reply', 'owner') or not exact_sentence(doc['text'], quote):
            raise ValueError('Each item needs an exact sentence from its agreement document.')
        amounts = {int(Decimal(m.replace(',', '')) * 100)
                   for m in re.findall(r'(?:USD\s*|\$\s*)(\d[\d,]*(?:\.\d{1,2})?)(?!\d|\.\d)', quote)}
        if amount not in amounts:
            raise ValueError('Every item amount must appear as USD or $ in its exact agreement quote.')
        if not name_words(item['name']) <= name_words(quote):
            raise ValueError('Use the agreed item name from the quote, not an invented item.')
        if re.search(r'\b(not|never|free|complimentary|included)\b', quote, re.I) or '?' in quote:
            raise ValueError('This quote is not an unambiguous billable agreement; ask the owner.')
        if any(amount == old_amount and similar_name(item['name'], old_name) for old_name, old_amount in checked):
            raise ValueError('Duplicate items within the proposal.')
        checked.append((item['name'], amount))
    return checked


def agreement_refs(action):
    return ['unstuck-agreement:' + hashlib.sha256(
        (item['quote_doc'] + '\n' + re.sub(r'\s+', ' ', item['quote']).strip()).encode()
    ).hexdigest()[:24] for item in action['items']]


def duplicate_item(items, invoices, references=()):
    for inv in invoices:
        for existing in inv.get('items', []):
            if existing.get('description') in references:
                return f"Already invoiced: agreement reference on {inv['id']}. Keep the existing invoice."
    # shortcut: lexical similarity, not semantic equivalence; ambiguous names need owner review.
    for inv in invoices:
        for existing in inv.get('items', []):
            if references and re.fullmatch(r'unstuck-agreement:[0-9a-f]{24}', existing.get('description', '')):
                continue  # A different recorded agreement is not a lexical duplicate.
            money = existing['unit_amount']
            if money['currency_code'] != 'USD':
                continue
            unit = Decimal(money['value']) * 100
            total = unit * Decimal(existing.get('quantity', '1'))
            for name, amount in items:
                if Decimal(amount) in (unit, total) and similar_name(name, existing.get('name', '')):
                    return f"Already invoiced: {name} on {inv['id']}. Keep the existing invoice."
    return None
