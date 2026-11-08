"""Independent expected decisions from the product policy; no provider calls."""
import json
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from daybreak.domain import (
    ConfirmedRequest, Delegation, ExistingJob, Order, Policy, Usage, decide,
)


def utc(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.req = ConfirmedRequest('mock-request', 1, 'refund', False, 3900,
                                    utc('2026-11-08T01:00:00Z'))
        self.order = Order('mock-order', 'mock-capture', 3900, 'USD',
                           utc('2026-11-15T12:00:00Z'))
        self.policy = Policy(3, 24)
        self.delegation = Delegation('mock-delegation', 5000, 10000,
                                     utc('2026-11-08T08:00:00Z'), None)
        self.now = utc('2026-11-08T01:00:00Z')

    def test_independent_policy_expectations(self):
        rows = [
            ('normal', {}, ('execute', 'ok')),
            ('exact_cutoff', {'received_at': '2026-11-14T12:00:00Z'}, ('execute', 'ok')),
            ('late', {'received_at': '2026-11-14T12:00:00.000001Z'}, ('rejected_policy', 'outside_window')),
            ('exact_caps', {'amount': 5000, 'held': 5000}, ('execute', 'ok')),
            ('per_cap', {'amount': 5001}, ('pending_approval', 'over_per_refund')),
            ('total_cap', {'amount': 2900, 'held': 8800}, ('pending_approval', 'over_total')),
            ('expiry', {'now': '2026-11-08T08:00:00Z'}, ('pending_approval', 'delegation_expired')),
            ('stop', {'stopped': True}, ('pending_approval', 'delegation_stopped')),
            ('absent', {'no_delegation': True}, ('pending_approval', 'no_delegation')),
            ('manual_cap', {'actor': 'seller', 'held': 10000}, ('execute', 'ok')),
            ('manual_late', {'actor': 'seller', 'received_at': '2026-11-14T12:00:01Z'}, ('rejected_policy', 'outside_window')),
            ('completed', {'existing': 'completed'}, ('link_existing', 'already_refunded')),
            ('executing', {'existing': 'executing'}, ('link_existing', 'in_progress')),
            ('unknown', {'existing': 'unknown'}, ('link_existing', 'result_unconfirmed')),
            ('definite_failure', {'existing': 'failed'}, ('execute', 'ok')),
            ('question', {'kind': 'question'}, ('needs_review', 'not_refund_request')),
            ('ambiguous', {'ambiguous': True}, ('needs_review', 'ambiguous_request')),
            ('currency', {'currency': 'EUR'}, ('needs_review', 'insufficient_order_facts')),
            ('mismatch', {'mismatch': True}, ('needs_review', 'insufficient_order_facts')),
        ]
        for name, changes, expected in rows:
            with self.subTest(name=name):
                amount = changes.get('amount', 3900)
                req = replace(self.req, amount_cents=amount,
                              received_at=utc(changes['received_at']) if 'received_at' in changes else self.req.received_at,
                              kind=changes.get('kind', 'refund'), ambiguous=changes.get('ambiguous', False))
                order = replace(self.order, amount_cents=amount + int(changes.get('mismatch', False)),
                                currency=changes.get('currency', 'USD'))
                deleg = None if changes.get('no_delegation') else self.delegation
                if changes.get('stopped'):
                    deleg = replace(deleg, stopped_at=self.now)
                existing = ExistingJob('mock-job', changes['existing'], None) if 'existing' in changes else None
                result = decide(req, order, self.policy, deleg, Usage(changes.get('held', 0)), existing,
                                evaluated_at=utc(changes['now']) if 'now' in changes else self.now,
                                actor=changes.get('actor', 'agent'))
                self.assertEqual((result.outcome, result.reason), expected)
                self.assertEqual(result.amount_cents, amount)
                if existing and existing.state != 'failed':
                    self.assertEqual(result.existing_job_id, existing.id)

    def test_catalog_is_complete_but_not_an_execution_report(self):
        catalog = json.loads(Path(__file__).with_name('cases.json').read_text())
        self.assertEqual(len(catalog['cases']), 24)
        self.assertEqual(len({c['id'] for c in catalog['cases']}), 24)
        self.assertEqual(catalog['status'], 'draft_expected_results_not_execution_results')
        for case in catalog['cases']:
            self.assertTrue(case['steps'])
            self.assertTrue(case['expected']['invariants'])
            self.assertIn('refund_calls', case['expected'])


if __name__ == '__main__':
    unittest.main()
