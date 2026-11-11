"""Public demo accounts, persistence, auth boundaries and cleanup; no network."""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from daybreak.logbook import Logbook
from unstuck import accounts, workspaces
from test_workspaces import public, build, SECRET  # noqa: F401
from test_unstuck import offline  # noqa: F401

PASSWORD = 'synthetic-not-a-real-password'


async def signup(call, client, email='owner@example.test'):
    response = await call(client, 'POST', '/signup', data={'email': email, 'password': PASSWORD})
    assert response.status_code == 303, response.text
    return workspaces.verify(SECRET, client.cookies.get(workspaces.COOKIE))


async def test_signup_logout_login_preserves_case_chats_and_home(public, monkeypatch, tmp_path):
    app, store, inv, call, visitor = public
    log = tmp_path / 'auth.jsonl'
    app.state.log = Logbook(str(log))
    async with visitor('192.0.2.1') as client:
        ws = await signup(call, client, '  OWNER@Example.Test ')
        r = workspaces.row(store, ws)
        assert r['expires'] - r['created'] == 45 * 86400
        case = store.cases(workspace=ws)[0]
        store.event(case['id'], 'owner_chat', 'private case chat', {'text': 'keep this case conversation'})
        monkeypatch.setattr(app.state.agent, 'call', AsyncMock(return_value={'content': [{'type': 'text', 'text': 'Saved home response'}]}))
        assert (await call(client, 'POST', '/home/chat', json={'text': 'Keep this home question'})).status_code == 200
        response = await call(client, 'POST', '/logout')
        assert response.status_code == 303 and not client.cookies.get(workspaces.COOKIE)
        assert (await call(client, 'GET', '/api/case/' + case['id'])).status_code == 303
        response = await call(client, 'POST', '/login', data={'email': ' OWNER@EXAMPLE.TEST ', 'password': PASSWORD})
        assert response.status_code == 303
        assert 'HttpOnly' in response.headers['set-cookie'] and 'SameSite=lax' in response.headers['set-cookie']
        assert workspaces.verify(SECRET, client.cookies.get(workspaces.COOKIE)) == ws
        home = (await call(client, 'GET', '/')).text
        assert 'owner@example.test' in home and 'Log out' in home and 'Keep this home question' in home
        case_response = (await call(client, 'GET', '/api/case/' + case['id'])).json()
        assert any(e['label'] == 'private case chat' for e in case_response['events'])
        assert len(inv.created) == 5
        with store.db() as db:
            row = db.execute('SELECT * FROM accounts').fetchone()
            assert row['hash'] != PASSWORD.encode() and len(row['salt']) == 32
            assert row['hash'] == accounts.password_hash(PASSWORD, row['salt'])
            dump = '\n'.join(db.iterdump())
        assert PASSWORD not in dump and PASSWORD not in log.read_text()


async def test_generic_error_failed_attempt_limit_and_duplicate(public):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as client:
        await signup(call, client)
        await call(client, 'POST', '/logout')
        r = await call(client, 'POST', '/signup', data={'email': 'OWNER@example.test', 'password': PASSWORD})
        assert r.status_code == 400 and 'already exists' in r.text and len(inv.created) == 5
        bodies = []
        for i in range(10):
            r = await call(client, 'POST', '/login', data={'email': 'owner@example.test' if i % 2 else 'unknown@test', 'password': 'incorrect'})
            assert r.status_code == 400 and 'Email or password is wrong.' in r.text
            bodies.append(r.text)
        assert len(set(bodies)) == 1
        r = await call(client, 'POST', '/login', data={'email': 'owner@example.test', 'password': PASSWORD})
        assert r.status_code == 429 and '15 minutes' in r.text
    async with visitor('192.0.2.2') as other:
        assert (await call(other, 'POST', '/login', data={'email': 'owner@example.test', 'password': PASSWORD})).status_code == 303


async def test_accounts_are_isolated(public, monkeypatch):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as a, visitor('192.0.2.2') as b:
        wa, wb = await signup(call, a, 'a@test'), await signup(call, b, 'b@test')
        case = store.cases(workspace=wa)[0]
        store.add_home_exchange(wa, 'A-only', {'reply': 'A-response', 'links': []})
        store.event(case['id'], 'owner_chat', 'A-only')
        q = store.add_question(case['id'], 'q', 'fake@test', 'Private question', 'h', [])
        for method, path in [('GET', '/case/' + case['id']), ('GET', '/api/case/' + case['id']),
                             ('POST', '/case/' + case['id'] + '/investigate'), ('POST', '/case/' + case['id'] + '/unbilled'),
                             ('POST', '/case/' + case['id'] + '/scope'), ('POST', '/case/' + case['id'] + '/chat'),
                             ('POST', f'/questions/{q}/approve')]:
            assert (await call(b, method, path)).status_code == 404
        monkeypatch.setattr(app.state.agent, 'call', AsyncMock(return_value={'content': [
            {'type': 'tool_use', 'id': 'x', 'name': 'investigate', 'input': {'client': case['id']}}]}))
        result = (await call(b, 'POST', '/home/chat', json={'text': 'Investigate'})).json()
        assert 'not found' in result['reply']
        home = (await call(b, 'GET', '/')).text
        assert 'A-only' not in home and case['id'] not in home and 'a@test' not in home
        assert store.case(case['id'])['state'] == 'new' and wa != wb


async def test_creation_caps_retention_expiry_and_reset(public):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as client:
        first = await signup(call, client, 'a@test')
        workspaces.purge_expired(store, time.time() + 49 * 3600)
        assert store.cases(workspace=first) and accounts.email_for(store, first) == 'a@test'
        await call(client, 'POST', '/logout')
        second = await signup(call, client, 'b@test')
        await call(client, 'POST', '/logout')
        r = await call(client, 'POST', '/signup', data={'email': 'c@test', 'password': PASSWORD})
        assert r.status_code == 429
        with store.db() as db:
            assert db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0] == 2
        workspaces.set_row(store, first, expires=time.time() - 1)
        workspaces.purge_expired(store, time.time())
        assert not accounts.email_for(store, first) and not store.cases(workspace=first)
        assert accounts.email_for(store, second)
        store.clear()
        with store.db() as db:
            assert db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM workspaces').fetchone()[0] == 0


async def test_seed_failure_and_interruption_can_signup_again(public):
    app, store, inv, call, visitor = public
    inv.fail_at = 2
    async with visitor('192.0.2.1') as client:
        failed = await signup(call, client)
        assert not accounts.email_for(store, failed)
        inv.fail_at = None
        good = await signup(call, client)
        assert accounts.email_for(store, good) == 'owner@example.test'
        assert failed != good
        workspaces.set_row(store, good, state='seeding')  # no running task: simulates process restart
        await call(client, 'POST', '/logout')
    async with visitor('192.0.2.2') as client:
        recovered = await signup(call, client)
        assert recovered != good and not accounts.email_for(store, good)


async def test_local_mode_has_no_auth_and_secure_cookie(public, monkeypatch, tmp_path):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as client:
        monkeypatch.setenv('RENDER', 'true')
        r = await call(client, 'POST', '/signup', data={'email': 'secure@test', 'password': PASSWORD})
        assert r.status_code == 303 and 'Secure' in r.headers['set-cookie']
    monkeypatch.delenv('RENDER')
    app, store, inv, call, visitor = build(monkeypatch, tmp_path, UNSTUCK_MODE='local')
    async with visitor('192.0.2.1') as client:
        home = (await call(client, 'GET', '/')).text
        assert 'Ask Unstuck' in home and 'Create account' not in home and 'Log out' not in home
        assert (await call(client, 'POST', '/login', data={})).status_code == 404
        assert (await call(client, 'POST', '/workspace')).status_code == 303


@pytest.mark.parametrize('email,password', [('bad', PASSWORD), ('x@y', 'short'), ('x@y', 'a' * 1025)])
async def test_bad_credentials_do_not_seed(public, email, password):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as client:
        assert (await call(client, 'POST', '/signup', data={'email': email, 'password': password})).status_code == 400
        assert not inv.created


async def test_cross_site_auth_rejected_and_random_salts(public):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as a:
        for path in ('/signup', '/login', '/logout'):
            assert (await call(a, 'POST', path, headers={'Origin': 'https://other.test'}, data={'email': 'a@test', 'password': PASSWORD})).status_code == 403
        await signup(call, a, 'a@test')
        await call(a, 'POST', '/logout')
        await signup(call, a, 'b@test')
        with store.db() as db:
            rows = db.execute('SELECT salt, hash FROM accounts').fetchall()
        assert rows[0]['salt'] != rows[1]['salt'] and rows[0]['hash'] != rows[1]['hash']


async def test_concurrent_duplicate_signup_is_atomic(public):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as a, visitor('192.0.2.2') as b:
        responses = await asyncio.gather(*[call(c, 'POST', '/signup', data={'email': 'same@test', 'password': PASSWORD}) for c in (a, b)])
        assert sorted(r.status_code for r in responses) == [303, 400]
        with store.db() as db:
            assert db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0] == 1
            assert db.execute('SELECT COUNT(*) FROM workspaces').fetchone()[0] == 1
        assert len(inv.created) == 5


async def test_daily_signup_cap_and_https_cookie(public):
    app, store, inv, call, visitor = public
    for i in range(4):
        async with visitor(f'192.0.2.{i+1}') as client:
            client.base_url = 'https://test'
            r = await call(client, 'POST', '/signup', data={'email': f'{i}@test', 'password': PASSWORD})
            assert r.status_code == (303 if i < 3 else 429)
            if i < 3:
                assert 'Secure' in r.headers['set-cookie']
    assert len(inv.created) == 15


@pytest.mark.parametrize('days', ['0', '-1', 'inf', 'nan'])
def test_account_retention_must_be_positive_finite(days):
    with pytest.raises(ValueError):
        workspaces.Settings({'UNSTUCK_ACCOUNT_DAYS': days})


async def test_success_does_not_erase_ip_failure_history(public):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as client:
        await signup(call, client)
        await call(client, 'POST', '/logout')
        for _ in range(9):
            assert (await call(client, 'POST', '/login', data={'email': 'unknown@test', 'password': PASSWORD})).status_code == 400
        assert (await call(client, 'POST', '/login', data={'email': 'owner@example.test', 'password': PASSWORD})).status_code == 303
        await call(client, 'POST', '/logout')
        assert (await call(client, 'POST', '/login', data={'email': 'unknown@test', 'password': PASSWORD})).status_code == 400
        assert (await call(client, 'POST', '/login', data={'email': 'owner@example.test', 'password': PASSWORD})).status_code == 429
