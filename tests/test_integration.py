"""Independent 24-scenario acceptance checks. All payments use HTTP MockTransport.

Fixture DB writes establish starting conditions only; actions under test use Service.
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from daybreak.dummy_paypal import DummyPayPal
from daybreak.execute import Service
from daybreak.extract import DummyExtractor
from daybreak.paypal import PayPalClient
from daybreak.ports import Extraction, StaleVersion, NotFound

CATALOG = json.loads(Path(__file__).with_name('cases.json').read_text())
NOW = datetime(2026, 11, 8, 1, tzinfo=timezone.utc)
START = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


class Clock:
    value = NOW
    def now(self): return self.value


class World:
    def __init__(self, path):
        self.db = str(path)
        self.clock = Clock()
        self.dummy = DummyPayPal()
        self.calls = []
        self.adapter_calls = 0
        self.extractor = DummyExtractor()
        self.client = PayPalClient('synthetic-id', 'synthetic-secret', transport=httpx.MockTransport(self.handle))
        original = self.client.refund
        async def counted(*args, **kwargs):
            self.adapter_calls += 1
            return await original(*args, **kwargs)
        self.client.refund = counted
        self.service = Service(self.db, self.client, self.extractor, self.clock)
        self.sql('INSERT INTO policies (version, full_refund_cutoff_hours) VALUES (3,24)')
        self.order('a', 3900)

    def handle(self, request):
        if request.url.path.endswith('/refund'):
            self.calls.append((request.headers['PayPal-Request-Id'], request.content))
            # A separate connection must acquire the write lock during the network call.
            with sqlite3.connect(self.db, timeout=0) as c:
                c.execute('BEGIN IMMEDIATE')
                assert c.execute('SELECT COUNT(*) FROM refund_attempts').fetchone()[0] >= 1
        return self.dummy._handle(request)

    def sql(self, query, args=()):
        with sqlite3.connect(self.db) as c:
            return c.execute(query, args).fetchall()

    def order(self, name, amount, currency='USD'):
        self.sql('INSERT INTO orders VALUES (?,?,?,?,?,?)',
                 (name, 'capture-'+name, amount, currency, 'Synthetic workshop '+name, START.isoformat()))
        self.dummy.add_capture('capture-'+name, amount, currency)

    async def delegate(self, per=5000, total=10000):
        return await self.service.start_delegation(per, total, NOW+timedelta(hours=7))

    async def submit(self, order='a', message='Please refund me'):
        d = await self.service.submit_message(order, message)
        return await self.service.confirm(d.draft_id, d.version, order)

    async def spent(self, amount):
        self.order('prior', amount)
        # A fixture records a previously allowed amount, independent of today's per-refund cap.
        self.sql("UPDATE delegations SET per_refund_limit_cents=10000")
        result = await self.submit('prior')
        self.sql("UPDATE delegations SET per_refund_limit_cents=5000")
        self.calls.clear(); self.adapter_calls = 0; self.dummy.refund_calls = 0
        return result

    def no_refund(self):
        assert self.adapter_calls == 0
        assert self.dummy.refund_calls == 0


@pytest.fixture
async def world(tmp_path):
    w = World(tmp_path/'evaluation.db')
    yield w
    await w.client.aclose()


@pytest.mark.parametrize('case', CATALOG['cases'], ids=lambda c: c['id']+'-'+c['title'])
async def test_catalog(case, world, monkeypatch):
    w = world
    n = int(case['id'])
    if n != 11:
        await w.delegate()
    if n == 1:
        r = await w.submit()
        assert r.job_state == 'completed'
        assert w.service.morning_summary().auto_completed_cents == 3900
        assert len(w.sql('SELECT * FROM refund_attempts')) == 1
    elif n in (2, 3, 4):
        cutoff = START-timedelta(hours=24)
        w.clock.value = cutoff
        w.sql('UPDATE delegations SET expires_at=?', ((START+timedelta(days=1)).isoformat(),))
        if n == 3: w.clock.value += timedelta(microseconds=1)
        if n == 4:
            w.clock.value = cutoff-timedelta(minutes=1)
            d = await w.service.submit_message('a', 'Please refund me')
            w.clock.value = cutoff+timedelta(seconds=1)
            r = await w.service.confirm(d.draft_id, d.version, 'a')
        else: r = await w.submit()
        if n == 2: assert r.job_state == 'completed'
        else:
            assert (r.status,r.reason) == ('rejected_policy','outside_window')
            w.no_refund()
    elif n == 5:
        await w.spent(5000)
        w.order('exact',5000)
        assert (await w.submit('exact')).job_state == 'completed'
        assert w.service.morning_summary().remaining_delegation_cents == 0
    elif n == 6:
        w.order('large',5001)
        r=await w.submit('large')
        assert (r.status,r.reason,r.job_id)==('pending_approval','over_per_refund',None)
        w.no_refund()
    elif n == 7:
        assert (await w.submit()).job_state=='completed'
        w.order('b',4900); w.order('c',2900)
        assert (await w.submit('b')).job_state=='completed'
        r=await w.submit('c')
        assert (r.status,r.reason,r.job_id)==('pending_approval','over_total',None)
        assert w.adapter_calls==2
        r=await w.service.approve(r.request_id)
        assert (r.job_state,r.actor)==('completed','seller')
        s=w.service.morning_summary()
        assert (s.auto_completed_cents,s.manual_completed_cents,s.remaining_delegation_cents)==(8800,2900,1200)
    elif n == 8:
        prior=await w.spent(8800)
        w.order('b',2900)
        for state in case['setup']['held_state_variants']:
            w.sql('UPDATE refund_jobs SET state=? WHERE id=?',(state,prior.job_id))
            r=await w.submit('b')
            assert (r.status,r.reason,r.job_id)==('pending_approval','over_total',None)
            assert w.service.morning_summary().remaining_delegation_cents==1200
        w.no_refund()
    elif n == 9:
        w.clock.value=NOW.replace(hour=8)
        r=await w.submit()
        assert (r.status,r.reason)==('pending_approval','delegation_expired')
        w.no_refund()
    elif n == 10:
        delegation=w.sql('SELECT id FROM delegations')[0][0]
        await w.service.stop_delegation(delegation)
        r=await w.submit()
        assert (r.status,r.reason)==('pending_approval','delegation_stopped')
        w.no_refund()
    elif n == 11:
        r=await w.submit()
        assert (r.status,r.reason,r.job_id)==('pending_approval','no_delegation',None)
        w.no_refund()
    elif n == 12:
        # Persist an eligible pending request then independently make its recorded receipt late.
        w.sql('UPDATE delegations SET per_refund_limit_cents=0')
        r=await w.submit()
        w.sql('UPDATE confirmed_requests SET received_at=?',((START-timedelta(hours=24)+timedelta(seconds=1)).isoformat(),))
        r=await w.service.approve(r.request_id)
        assert (r.status,r.reason)==('rejected_policy','outside_window')
        w.no_refund()
    elif n == 13:
        d=await w.service.submit_message('a','Please refund me')
        first=await w.service.confirm(d.draft_id,d.version,'a')
        again=await w.service.confirm(d.draft_id,d.version,'a')
        new=await w.submit()
        assert first.request_id==again.request_id
        assert (new.status,new.reason,new.refund_id)==('linked','already_refunded',first.refund_id)
        assert len(w.sql('SELECT * FROM refund_jobs'))==1
    elif n == 14:
        second=Service(w.db,w.client,w.extractor,w.clock)
        d1,d2=await asyncio.gather(w.service.submit_message('a','Please refund me'),second.submit_message('a','Please refund me'))
        results=await asyncio.gather(w.service.confirm(d1.draft_id,d1.version,'a'),second.confirm(d2.draft_id,d2.version,'a'))
        assert len({r.job_id for r in results})==1
        assert len(w.sql('SELECT * FROM refund_jobs'))==1
        assert w.dummy.money_out_calls==1
    elif n == 15:
        await w.spent(3900)
        w.order('b',4900);w.order('c',4900)
        results=await asyncio.gather(w.submit('b'),w.submit('c'))
        assert sorted(r.reason for r in results)==['ok','over_total']
        assert w.service.morning_summary().remaining_delegation_cents==1200
    elif n == 16:
        d=await w.service.submit_message('a','Please refund me')
        await w.service.edit_message(d.draft_id,'a','Refund please')
        with pytest.raises(StaleVersion): await w.service.confirm(d.draft_id,d.version,'a')
        assert not w.sql('SELECT * FROM confirmed_requests')
        w.no_refund()
    elif n == 17:
        # HTTP authorization variants are exercised in test_web; ownership is checked here too.
        d=await w.service.submit_message('a','Please refund me')
        w.order('b',3900)
        with pytest.raises(NotFound): await w.service.confirm(d.draft_id,d.version,'b')
        assert not w.sql('SELECT * FROM confirmed_requests')
        w.no_refund()
    elif n == 18:
        w.dummy.faults=['REFUND_NOT_ALLOWED']
        first=await w.submit()
        assert first.job_state=='failed'
        assert w.service.morning_summary().remaining_delegation_cents==10000
        second=await w.submit()
        assert (second.job_state,second.job_id)==('completed',first.job_id)
        assert len(w.sql('SELECT * FROM refund_attempts'))==2
        assert len({key for key,body in w.calls})==2
    elif n == 19:
        w.dummy.faults=['pending']
        first=await w.submit()
        await w.service.reconcile_job(first.job_id)
        assert w.service.get_case(first.request_id).job_state=='paypal_pending'
        assert (await w.submit()).reason=='in_progress'
        assert w.service.morning_summary().auto_completed_cents==0
        assert w.service.morning_summary().remaining_delegation_cents==6100
    elif n == 20:
        w.dummy.faults=['lost']
        first=await w.submit()
        assert first.job_state=='unknown'
        assert (await w.submit()).reason=='result_unconfirmed'
        assert w.service.morning_summary().remaining_delegation_cents==6100
        assert w.service.morning_summary().auto_completed_cents==0
        await w.service.reconcile_job(first.job_id)
        assert len(set(w.calls))==1
        assert w.dummy.money_out_calls==1
        assert w.service.get_case(first.request_id).job_state=='completed'
    elif n == 21:
        w.dummy.faults=['timeout']
        r=await w.submit()
        w.sql("UPDATE refund_jobs SET state='executing'")
        keys=w.sql('SELECT paypal_request_id FROM refund_attempts')
        w.calls.clear();w.adapter_calls=0;w.dummy.refund_calls=0
        restarted=Service(w.db,w.client,w.extractor,w.clock)
        assert restarted.recover_on_start()==1
        assert restarted.get_case(r.request_id).job_state=='unknown'
        assert restarted.morning_summary().remaining_delegation_cents==6100
        assert keys==w.sql('SELECT paypal_request_id FROM refund_attempts')
        w.no_refund()
    elif n == 22:
        # Restoring a consistent snapshot must pause even without interrupted jobs in that snapshot.
        restored=Path(w.db).with_name('restored.db')
        with sqlite3.connect(w.db) as src, sqlite3.connect(restored) as dst: src.backup(dst)
        w.service=Service(str(restored),w.client,w.extractor,w.clock)
        w.service.mark_restored()
        w.service.recover_on_start()
        await w.submit()
        w.no_refund()
    elif n == 23:
        class Broken:
            async def extract(self,*args): raise ValueError('synthetic malformed model output')
        class Timeout:
            async def extract(self,*args): raise TimeoutError('synthetic model timeout')
        class Ambiguous:
            async def extract(self,*args): return Extraction('refund',True,'Unclear request')
        for extractor,reason in [(Broken(),'not_refund_request'),(Timeout(),'not_refund_request'),(Ambiguous(),'ambiguous_request'),(DummyExtractor(),'not_refund_request')]:
            w.service=Service(w.db,w.client,extractor,w.clock)
            r=await w.submit(message='What is the refund policy?')
            assert (r.status,r.reason)==('needs_review',reason)
        w.service=Service(w.db,w.client,DummyExtractor(),w.clock)
        for change in ('missing','currency','amount','status'):
            w.dummy.add_capture('capture-a',3900)
            if change=='missing': del w.dummy.captures['capture-a']
            if change=='currency': w.dummy.captures['capture-a']['amount']['currency_code']='EUR'
            if change=='amount': w.dummy.captures['capture-a']['amount']['value']='29.00'
            if change=='status': w.dummy.captures['capture-a']['status']='PENDING'
            r=await w.submit()
            assert (r.status,r.reason)==('needs_review','insufficient_order_facts')
        w.no_refund()
    elif n == 24:
        await w.spent(8800)
        w.order('b',2900)
        r=await w.submit('b')
        assert r.reason=='over_total'
        w.dummy.captures['capture-b']['status']='REFUNDED'
        r=await w.service.approve(r.request_id)
        w.no_refund()
        assert r.refund_id is None
        assert r.reason=='already_refunded'
    expected=case['expected']['refund_calls']
    if isinstance(expected,int): assert w.adapter_calls==expected
    else: assert expected['min']<=w.adapter_calls<=expected['max']


async def test_unknown_retry_outside_retention_must_not_send(world):
    w=world
    await w.delegate()
    w.dummy.faults=['lost']
    r=await w.submit()
    w.clock.value=NOW+timedelta(days=400)
    calls=w.adapter_calls
    await w.service.reconcile_job(r.job_id)
    assert w.adapter_calls==calls, 'No documented idempotency retention covers a 400-day retry'


async def test_pending_eligibility_requires_current_capture_facts(world):
    w=world
    await w.delegate(per=0)
    del w.dummy.captures['capture-a']
    r=await w.submit()
    assert (r.status,r.reason)==('needs_review','insufficient_order_facts')
    w.no_refund()
