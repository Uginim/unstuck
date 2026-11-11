"""Conservative lexical check for outgoing drafts, not a general factuality classifier."""
import re

from .scenarios import FREELANCER
from .unbilled import exact_sentence

CLAIM = re.compile(
    r"\b(?:attached|attaching|enclosed)\b"
    r"|\bI(?:\s+have|['’]ve)\s+(?:sent|submitted|uploaded|updated|completed|signed|paid)\b"
    r"|\bhas\s+been\s+(?:updated|submitted|sent)\b", re.I)
ATTACHMENT = re.compile(r"\b(?:attached|attaching|enclosed)\b", re.I)
FIRST_PERSON = re.compile(r"\bI(?:\s+have|['’]ve)\b", re.I)


def draft_claim_error(action, docs, executed):
    # shortcut: English lexical patterns and exact sentence evidence only; expand on observed misses.
    # This does not infer actors, dates, negation or paraphrases; uncertain claims must be rewritten.
    if action['type'] not in ('draft_reply', 'send_reminder'):
        return None
    text = '\n'.join(str(action.get(k, '')) for k in (
        ('subject', 'body') if action['type'] == 'draft_reply' else ('note',)))
    sources = list(docs.values())
    client = [d['text'] for d in sources if d.get('kind') in ('mail', 'reply')
              and f"From: {FREELANCER['email']}" not in d['text']]
    # A quotation is exempt only if its words really occur in a client source.
    text = re.sub(r'"([^"\n]+)"|“([^”\n]+)”',
                  lambda m: '' if any(exact_sentence(s, m[1] or m[2]) for s in client) else m[0], text)
    for sentence in re.split(r'(?<=[.!?])\s+|\n+', text):
        # Scope drafts label exact source excerpts rather than asserting freelancer actions.
        prefix, sep, quote = sentence.partition(': ')
        if sep and prefix in ('Your request', 'Contract', 'Price basis', 'Timing') and any(
                exact_sentence(d['text'], quote) for d in sources):
            continue
        if not CLAIM.search(sentence):
            continue
        if ATTACHMENT.search(sentence):
            return 'Rejected: the app includes no attachments. Rewrite the draft/note as a request or next step.'
        evidence = [d['text'] for d in sources if not FIRST_PERSON.search(sentence)
                    or d.get('kind') == 'work' or f"From: {FREELANCER['email']}" in d['text']]
        # Only results of completed executions count; proposed params and prepared emails do not.
        evidence.extend(value for a in executed if a['state'] == 'done' and isinstance(a.get('result'), dict)
                        for value in a['result'].values() if isinstance(value, str))
        if not any(exact_sentence(s, sentence.strip()) for s in evidence):
            return ('Rejected: unsupported completed-action claim in draft/note. Rewrite as a request or '
                    'next step, or use the exact recorded completion sentence; never invent sent mail or work done.')
    return None
