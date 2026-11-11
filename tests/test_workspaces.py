"""Per-visitor workspaces: isolation, signed cookie, creation caps, expiry, seeding failure, local mode."""
import html
import importlib
import time

import httpx
import pytest

from daybreak.logbook import Logbook
from unstuck import workspaces
from unstuck.scenarios import SCENARIOS
from unstuck.store import Store
from test_unstuck import offline  # noqa: F401  (autouse: no real HTTP or model calls)

SECRET = 's' * 40


def raw_invoice(iid, number, email, cents):
    value = f'{cents / 100:.2f}'
    return {'id': iid, 'status': 'SENT', 'detail': {'invoice_number': number, 'currency_code': 'USD'},
            'amount': {'currency_code': 'USD', 'value': value}, 'due_amount': {'currency_code': 'USD', 'value': value},
            'payments': {'paid_amount': {'currency_code': 'USD', 'value': '0.00'}},
            'items': [{'name': 'Work', 'quantity': '1', 'unit_amount': {'currency_code': 'USD', 'value': value}}],
            'primary_recipients': [{'billing_info': {'email_address': email}}],
            'configuration': {'partial_payment': {'allow_partial_payment': False}}}


class FakeInvoicing:
    """Records every PayPal write; fails the Nth invoice creation when asked."""
    def __init__(self):
        self.invoices, self.created, self.cancelled, self.reminded, self.fail_at = {}, [], [], [], None

    async def create_and_send(self, *, number, recipient_email, recipient_name, items, note, **_):
        if self.fail_at == len(self.created) + 1:
            raise RuntimeError('sandbox rejected the invoice')
        iid = f'INV-{len(self.created) + 1}'
        self.created.append((iid, number, recipient_email))
        self.invoices[iid] = raw_invoice(iid, number, recipient_email, sum(c for _, c in items))
        return self.invoices[iid]

    async def record_external_payment(self, *args, **kwargs):
        pass

    async def get(self, iid):
        return self.invoices[iid]

    async def cancel(self, iid):
        self.cancelled.append(iid)

    async def remind(self, iid, subject, note):
        self.reminded.append(iid)


def build(monkeypatch, tmp_path, **env):
    module = importlib.import_module('unstuck.app')
    for key, value in {'PAYPAL_CLIENT_ID': 'fake-id', 'PAYPAL_CLIENT_SECRET': 'fake-secret',
                       'UNSTUCK_SWEEP_HOURS': '0', **env}.items():
        monkeypatch.setenv(key, value)
    inv, store = FakeInvoicing(), Store(tmp_path / 'ws.db')
    monkeypatch.setattr(module, 'PayPalClient', lambda *a, **k: object())
    monkeypatch.setattr(module, 'Invoicing', lambda _: inv)
    monkeypatch.setattr(module, 'Store', lambda _: store)
    monkeypatch.setattr(module, 'Logbook', lambda _: Logbook(None))
    spawned = []
    monkeypatch.setattr(module, 'spawn', spawned.append)
    app = module.create_app()

    async def call(client, method, path, **kwargs):
        response = await client.request(method, path, **kwargs)
        while spawned:  # seeding and agent runs finish before the test looks
            coro = spawned.pop(0)
            if coro.cr_code.co_name == 'seed':
                await coro
            else:
                coro.close()
        return response

    def visitor(ip):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 1234)), base_url='http://test')
    return app, store, inv, call, visitor


@pytest.fixture
def public(monkeypatch, tmp_path):
    return build(monkeypatch, tmp_path, UNSTUCK_MODE='public', UNSTUCK_SESSION_SECRET=SECRET,
                 UNSTUCK_WORKSPACES_PER_IP='2', UNSTUCK_WORKSPACES_PER_DAY='3')


async def started(call, client):
    assert (await call(client, 'POST', '/workspace')).status_code == 303
    return workspaces.verify(SECRET, client.cookies.get(workspaces.COOKIE))


async def test_each_visitor_gets_own_fresh_invoices(public):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as a, visitor('192.0.2.2') as b:
        wa, wb = await started(call, a), await started(call, b)
        assert wa and wb and wa != wb
        assert len(inv.created) == 2 * len(SCENARIOS)
        mine = lambda ws: {email for iid, _, email in inv.created if iid in {c['invoice_id'] for c in store.cases(workspace=ws)}}
        assert all(f'+{wa}@' in e for e in mine(wa)) and all(f'+{wb}@' in e for e in mine(wb))
        assert len({number for _, number, _ in inv.created}) == len(inv.created)
        home_a = (await call(a, 'GET', '/')).text
        assert all(f'/case/{c["id"]}' in home_a for c in store.cases(workspace=wa))
        assert not any(f'/case/{c["id"]}' in home_a for c in store.cases(workspace=wb))


async def test_other_workspace_ids_are_not_found_and_untouched(public):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as a, visitor('192.0.2.2') as b:
        wa = await started(call, a)
        await started(call, b)
        case = store.cases(workspace=wa)[0]
        qid = store.add_question(case['id'], 'tu', 'ap@client.example', 'SECRET-A question?', 'h', [])
        store.set_question(qid, state='sent')
        store.add_action(case['id'], 'send_reminder', {'type': 'send_reminder', 'why': 'Due', 'note': 'Please pay.'})
        aid = store.actions(case['id'])[0]['id']
        store.save_case(case['id'], state='done', final={'status': 'NEEDS_OWNER', 'cause': 'SECRET-A cause.',
                                                         'summary_for_owner': 'SECRET-A summary.'})
        for method, path, data in [
            ('GET', f'/case/{case["id"]}', None), ('GET', f'/api/case/{case["id"]}', None),
            ('POST', f'/case/{case["id"]}/investigate', None), ('POST', f'/case/{case["id"]}/chat', {'message': 'hi'}),
            ('POST', f'/case/{case["id"]}/documents', {'title': 't', 'text': 'x'}),
            ('POST', f'/case/{case["id"]}/scope', None), ('POST', f'/case/{case["id"]}/unbilled', None),
            ('POST', f'/questions/{qid}/reply', {'reply': 'B answers'}), ('POST', f'/questions/{qid}/skip', None),
            ('POST', f'/actions/{aid}/approve', None), ('POST', f'/actions/{aid}/dismiss', None),
            ('GET', '/case/does-not-exist', None),
        ]:
            response = await call(b, method, path, data=data)
            assert response.status_code == 404, (method, path, response.status_code)
        assert store.question(qid)['state'] == 'sent' and store.action(aid)['state'] == 'proposed'
        assert store.case(case['id'])['state'] == 'done' and inv.reminded == []
        assert len(store.cases()) == 2 * len(SCENARIOS)  # no check case was created for A by B
        for page in ('/', '/inbox'):
            assert 'SECRET-A' not in (await call(b, 'GET', page)).text
        assert 'SECRET-A' in (await call(a, 'GET', '/')).text
        assert 'SECRET-A question?' in (await call(a, 'GET', '/inbox')).text
        # The owner can act on their own action; only their own invoice is touched.
        assert (await call(a, 'POST', f'/actions/{aid}/approve')).status_code == 200
        assert inv.reminded == [case['invoice_id']]


@pytest.mark.parametrize('tamper', ['other_id', 'bad_mac', 'other_secret', 'garbage', 'none'])
async def test_tampered_cookie_gets_no_workspace(public, tamper):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as a, visitor('192.0.2.2') as b:
        wa = await started(call, a)
        case = store.cases(workspace=wa)[0]
        good = a.cookies.get(workspaces.COOKIE)
        forged = {'other_id': 'ffffffffffff.' + good.split('.')[1],
                  'bad_mac': wa + '.' + '0' * 32,
                  'other_secret': workspaces.sign('x' * 40, wa),
                  'garbage': '../../' + good, 'none': None}[tamper]
        if forged:
            b.cookies.set(workspaces.COOKIE, forged)
        assert (await call(b, 'GET', f'/api/case/{case["id"]}')).status_code == 303
        assert (await call(b, 'POST', f'/case/{case["id"]}/investigate')).status_code == 404
        home = (await call(b, 'GET', '/')).text
        assert 'Start my demo' in home and case['id'] not in home


async def test_creation_caps_per_ip_and_per_day(public):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as a1, visitor('192.0.2.1') as a2, visitor('192.0.2.1') as a3:
        await started(call, a1)
        await started(call, a2)
        blocked = await call(a3, 'POST', '/workspace')
        assert blocked.status_code == 429 and '/sample' in blocked.text and workspaces.COOKIE not in a3.cookies
    async with visitor('192.0.2.2') as b, visitor('192.0.2.3') as c:
        await started(call, b)
        blocked = await call(c, 'POST', '/workspace')
        assert blocked.status_code == 429 and 'limit of new visitors' in blocked.text
    with store.db() as db:
        assert db.execute('SELECT COUNT(*) FROM workspaces').fetchone()[0] == 3
    async with visitor('192.0.2.4') as d:
        sample = await call(d, 'GET', '/sample')
        assert sample.status_code == 200 and all(html.escape(s['client']) in sample.text for s in SCENARIOS)
    assert len(inv.created) == 3 * len(SCENARIOS)


async def test_expired_workspace_is_closed_purged_and_not_reused(public):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as a:
        old = await started(call, a)
        case = store.cases(workspace=old)[0]
        with store.db() as db:
            db.execute('UPDATE workspaces SET expires=? WHERE id=?', (time.time() - 1, old))
        home = await call(a, 'GET', '/')
        assert 'expired' in home.text and 'Start my demo' in home.text
        assert f'{workspaces.COOKIE}=""' in home.headers['set-cookie']
        assert (await call(a, 'GET', f'/case/{case["id"]}')).status_code == 303
        new = await started(call, a)
        assert new != old
        assert store.cases(workspace=old) == [] and store.case(case['id']) is None
        assert workspaces.row(store, old)['state'] == 'expired'


async def test_seeding_failure_cancels_partial_invoices_and_offers_retry(public):
    app, store, inv, call, visitor = public
    inv.fail_at = 3
    async with visitor('192.0.2.1') as a:
        ws = await started(call, a)
        assert workspaces.row(store, ws)['state'] == 'failed'
        assert inv.cancelled == ['INV-1', 'INV-2'] and store.cases(workspace=ws) == []
        home = await call(a, 'GET', '/')
        assert 'did not accept' in home.text and 'Start my demo' in home.text
        assert (await call(a, 'GET', '/inbox')).status_code == 303
        inv.fail_at = None
        retry = await started(call, a)
        assert retry != ws and workspaces.row(store, retry)['state'] == 'ready'


async def test_seeding_progress_and_interrupted_seed(public):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as a:
        ws = await started(call, a)
        workspaces.set_row(store, ws, state='seeding', seeded=2)
        app.state.seeding.add(ws)  # still running in this process
        page = (await call(a, 'GET', '/')).text
        assert '2 of 5' in page and (await call(a, 'GET', '/inbox')).status_code == 303
        app.state.seeding.discard(ws)  # e.g. the server restarted mid-seed
        assert 'stopped before it finished' in (await call(a, 'GET', '/')).text


def test_scenario_book_aliases_and_keeps_copies_apart(tmp_path):
    book = workspaces.ScenarioBook({s['key']: s for s in SCENARIOS})
    base = book['signal']['recipient'][1]
    a, b = book['signal~aaaaaaaaaaaa'], book['signal~bbbbbbbbbbbb']
    assert a['recipient'][1] == base.replace('@', '+aaaaaaaaaaaa@') and book['signal']['recipient'][1] == base
    a['docs'].append({'id': 'x'})
    assert len(b['docs']) == len(book['signal']['docs']) == len(a['docs']) - 1
    with pytest.raises(KeyError):
        book['missing~aaaaaaaaaaaa']
    store = Store(tmp_path / 'db')
    store.add_case('c1', 'signal~aaaaaaaaaaaa', 'INV', 'Client', 'Title')
    source = store.case('c1')
    assert source['workspace'] == 'aaaaaaaaaaaa'
    assert store.unbilled_case(source, a['recipient'][1], create=True)['workspace'] == 'aaaaaaaaaaaa'
    assert store.scope_case(source, create=True)['workspace'] == 'aaaaaaaaaaaa'
    assert [c['id'] for c in store.cases(workspace='aaaaaaaaaaaa')] == ['c1'] and store.cases(workspace='') == []


async def test_local_mode_keeps_one_shared_workspace(monkeypatch, tmp_path):
    monkeypatch.delenv('UNSTUCK_MODE', raising=False)
    app, store, inv, call, visitor = build(monkeypatch, tmp_path)
    store.add_case('local1', 'harbor', 'INV-1', 'Harbor & Vine', 'Brand guidelines')
    inv.invoices['INV-1'] = raw_invoice('INV-1', 'MP-1', 'jordan.lee@harborvine.example.com', 148000)
    async with visitor('192.0.2.1') as a:
        assert '/case/local1' in (await call(a, 'GET', '/')).text
        assert (await call(a, 'GET', '/case/local1')).status_code == 200
        assert (await call(a, 'POST', '/workspace')).status_code == 303
    assert inv.created == [] and workspaces.COOKIE not in a.cookies
    assert store.case('local1')['workspace'] == ''


def test_public_mode_requires_a_session_secret(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match='SESSION_SECRET'):
        build(monkeypatch, tmp_path, UNSTUCK_MODE='public', UNSTUCK_SESSION_SECRET='short')
