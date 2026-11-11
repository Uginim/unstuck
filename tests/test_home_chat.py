"""Home chat: model decisions are untrusted; only shared, workspace-scoped starts run."""
import json
from unittest.mock import AsyncMock

import pytest

from test_workspaces import public, started  # noqa: F401
from test_unstuck import offline  # noqa: F401
from unstuck.home_chat import TOOLS


def choice(name, **args):
    return {'content': [{'type': 'tool_use', 'id': 'tool-1', 'name': name, 'input': args}]}


def model(app, monkeypatch, raw):
    call = AsyncMock(return_value=raw)
    monkeypatch.setattr(app.state.agent, 'call', call)
    return call


@pytest.mark.parametrize('name,kind,route', [
    ('investigate', 'investigate', 'investigate'), ('check_unbilled', 'unbilled', 'unbilled'),
    ('check_new_requests', 'scope', 'scope'), ('check_all_invoices', 'sweep', None),
    ('check_unbilled', 'sweep', None)])
async def test_tools_and_buttons_share_start(public, monkeypatch, name, kind, route):
    app, store, inv, call, visitor = public
    # The route closure uses the passed helper; observe its real effects, then the button's same guard.
    async with visitor('192.0.2.1') as client:
        ws = await started(call, client)
        c = store.cases(workspace=ws)[0]
        args = {} if name == 'check_all_invoices' else {'client': 'all' if kind == 'sweep' else c['id']}
        mock = model(app, monkeypatch, choice(name, **args))
        writes = len(inv.created)
        r = await call(client, 'POST', '/home/chat', json={'text': 'Check my work'})
        assert r.status_code == 200 and 'Check started' in r.json()['reply']
        assert mock.await_count == 1
        assert len(inv.created) == writes and not inv.reminded
        href = r.json()['links'][0]['href']
        if route:
            state = store.case(href.split('/')[-1])
            assert state['mode'] == kind and state['state'] == 'running'
            button = await call(client, 'POST', f'/case/{c["id"]}/{route}')
            assert button.status_code == (409 if kind == 'investigate' else 303)
            if kind != 'investigate':
                assert button.headers['location'] == href
        else:
            assert (await call(client, 'POST', '/sweep')).status_code == 409
        refused = (await call(client, 'POST', '/home/chat', json={'text': 'Check again'})).json()
        assert 'already' in refused['reply'] and refused['links'][0]['href'] == href


async def test_unknown_cross_workspace_and_ambiguity(public, monkeypatch):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as a, visitor('192.0.2.2') as b:
        wa, wb = await started(call, a), await started(call, b)
        other = store.cases(workspace=wb)[0]
        for target in ('missing', other['id']):
            model(app, monkeypatch, choice('investigate', client=target))
            r = await call(a, 'POST', '/home/chat', json={'text': 'Investigate this client'})
            assert 'not found' in r.json()['reply']
        mine = store.cases(workspace=wa)[0]
        store.add_case('duplicate', mine['scenario'], mine['invoice_id'], mine['client'], 'second')
        model(app, monkeypatch, choice('investigate', client=mine['client']))
        assert 'Which client' in (await call(a, 'POST', '/home/chat', json={'text': 'Check client'})).json()['reply']
        model(app, monkeypatch, {'content': [{'type': 'text', 'text': 'Which client do you mean?'}]})
        await call(a, 'POST', '/home/chat', json={'text': 'Why are they late?'})
        assert all(c['state'] == 'new' for c in store.cases())
        assert not store.home_exchanges(wb)
        for exchange in store.home_exchanges(wa):
            assert other['id'] not in json.dumps(exchange['links'])


async def test_pending_guard_korean_and_context(public, monkeypatch):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as a:
        ws = await started(call, a)
        c = store.cases(workspace=ws)[0]
        store.add_action(c['id'], 'draft_reply', {'body': 'review'})
        mock = model(app, monkeypatch, choice('investigate', client=c['id']))
        await call(a, 'GET', '/')
        result = (await call(a, 'POST', '/home/chat', json={'text': '왜 아직 돈이 안 들어왔어?'})).json()
        assert '대기 중인 작업' in result['reply']
        assert result['links'][0]['href'] == '/case/' + c['id']
        assert store.case(c['id'])['state'] == 'new'
        assert 'current_brief' in mock.call_args.kwargs['system']
        assert 'Needs your approval' in mock.call_args.kwargs['system']
        assert mock.call_args.kwargs['tools'] == TOOLS
        model(app, monkeypatch, choice('check_unbilled', client=c['id']))
        result = (await call(a, 'POST', '/home/chat', json={'text': '청구 빠진 거 있어?'})).json()
        assert '확인 작업을 시작' in result['reply']


@pytest.mark.parametrize('name', ['approve', 'send_reminder', 'create_invoice', 'allow_partial_payment'])
async def test_no_financial_or_approval_tool(public, monkeypatch, name):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as a:
        ws = await started(call, a)
        c = store.cases(workspace=ws)[0]
        model(app, monkeypatch, choice(name, client=c['id']))
        result = (await call(a, 'POST', '/home/chat', json={'text': 'Do the operation'})).json()
        assert 'Unsupported' in result['reply']
        assert all(c['state'] == 'new' for c in store.cases())
        assert not store.actions(c['id']) and not inv.reminded


async def test_send_request_never_starts_even_on_wrong_tool(public, monkeypatch):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as a:
        ws = await started(call, a)
        c = store.cases(workspace=ws)[0]
        model(app, monkeypatch, choice('investigate', client=c['id']))
        result = (await call(a, 'POST', '/home/chat', json={'text': 'Send the Harbor question'})).json()
        assert 'cannot approve or send' in result['reply'] and result['links']
        assert store.case(c['id'])['state'] == 'new'


async def test_limits_public_allowlist_and_escaped_persisted_history(public, monkeypatch):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as a:
        assert (await call(a, 'POST', '/home/chat', json={'text': 'Hi'})).status_code == 404
        ws = await started(call, a)
        mock = model(app, monkeypatch, {'content': [{'type': 'text', 'text': '<script>alert(1)</script>'}]})
        assert (await call(a, 'POST', '/unknown')).status_code == 404
        for _ in range(19):  # the rejected unauthenticated request also used one IP slot
            assert (await call(a, 'POST', '/home/chat', json={'text': '<img src=x onerror=alert(1)>'})).status_code == 200
        assert (await call(a, 'POST', '/home/chat', json={'text': 'Hi'})).status_code == 429
        assert mock.await_count == 19
        page = (await call(a, 'GET', '/')).text
        assert '&lt;script&gt;' in page and '<script>alert(1)</script>' not in page
        assert '&lt;img src=x' in page
        assert len(store.home_exchanges(ws)) == 6
    async with visitor('192.0.2.2') as b:
        await started(call, b)
        store.add_spend('none', 100)
        assert (await call(b, 'POST', '/home/chat', json={'text': 'Hi'})).status_code == 429
        assert mock.await_count == 19


async def test_invalid_output_and_input_start_nothing(public, monkeypatch):
    app, store, inv, call, visitor = public
    async with visitor('192.0.2.1') as a:
        await started(call, a)
        for body in (None, [], {'text': 1}, {'text': 'x' * 2001}, {'text': ''}):
            assert (await call(a, 'POST', '/home/chat', json=body)).status_code == 400
        c = store.cases()[0]
        for raw in (choice('investigate', client=c['id'], approve=True),
                    {'content': choice('investigate', client=c['id'])['content'] * 2},
                    {'content': []}):
            model(app, monkeypatch, raw)
            assert (await call(a, 'POST', '/home/chat', json={'text': 'Check'})).status_code == 200
        assert all(c['state'] == 'new' for c in store.cases())


async def test_full_private_log_and_budget_failure(public, monkeypatch, tmp_path):
    from daybreak.logbook import Logbook
    from unstuck.agent import BudgetExceeded
    app, store, inv, call, visitor = public
    log_path = tmp_path / 'home.jsonl'
    app.state.log = Logbook(str(log_path))
    async with visitor('192.0.2.1') as a:
        await started(call, a)
        text = '가' * 1999
        output = '답변' * 500
        mock = model(app, monkeypatch, {'content': [{'type': 'text', 'text': output}]})
        await call(a, 'POST', '/home/chat', json={'text': text})
        rows = [json.loads(line) for line in log_path.read_text().splitlines()]
        assert rows[0]['text'] == text and rows[-1]['model']['content'][0]['text'] == output
        assert rows[0]['request_id'] == rows[-1]['request_id']
        mock.side_effect = BudgetExceeded('next call')
        r = (await call(a, 'POST', '/home/chat', json={'text': '확인해 줘'})).json()
        assert '예산' in r['reply']
        assert all(c['state'] == 'new' for c in store.cases())
