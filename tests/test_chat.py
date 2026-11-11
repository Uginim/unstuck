"""Owner chat: transcript validity in every case state, chat is never approval, limits and escaping."""
import importlib
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from daybreak.logbook import Logbook
from unstuck import chat
from test_unstuck import answer, offline, question, script, tool, world  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parents[1]


def text(t):
    return {'type': 'text', 'text': t}


@pytest.fixture
def chat_app(world, monkeypatch):
    store, inv, agent = world
    module = importlib.import_module('unstuck.app')
    monkeypatch.setenv('PAYPAL_CLIENT_ID', 'fake-id')
    monkeypatch.setenv('PAYPAL_CLIENT_SECRET', 'fake-secret')
    monkeypatch.setenv('UNSTUCK_SWEEP_HOURS', '0')
    monkeypatch.setattr(module, 'PayPalClient', lambda *a, **k: object())
    monkeypatch.setattr(module, 'Invoicing', lambda _: inv)
    monkeypatch.setattr(module, 'Store', lambda _: store)
    monkeypatch.setattr(module, 'Logbook', lambda _: Logbook(None))
    app = module.create_app()
    app.state.agent = agent
    spawned = []
    monkeypatch.setattr(module, 'spawn', spawned.append)

    async def post(path, **data):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(path, data=data)
        while spawned:
            await spawned.pop(0)
        return response

    async def say(message, case='case'):
        return await post(f'/case/{case}/chat', message=message)

    return store, inv, agent, say, post


def assert_valid(messages):
    """Every tool_use is answered by exactly one tool_result in the next user message, results first."""
    assert messages[0]['role'] == 'user'
    for prev, nxt in zip(messages, messages[1:]):
        assert prev['role'] != nxt['role']
        if prev['role'] != 'assistant':
            continue
        content = nxt['content'] if isinstance(nxt['content'], list) else [text(nxt['content'])]
        uses = sorted(b['id'] for b in prev['content'] if b.get('type') == 'tool_use')
        assert sorted(b['tool_use_id'] for b in content if b['type'] == 'tool_result') == uses
        kinds = [b['type'] for b in content]
        assert kinds == sorted(kinds, key=lambda k: k != 'tool_result')


def owner_messages(store):
    return [b['text'] for m in store.case('case')['messages'] if m['role'] == 'user' and isinstance(m['content'], list)
            for b in m['content'] if b.get('type') == 'text' and b['text'].startswith(chat.PREFIX)]


def kinds(store):
    return [e['kind'] for e in store.events('case')]


async def test_done_closes_final_answer_then_text_reply_keeps_conclusion(chat_app):
    store, _, agent, say, _ = chat_app
    script(agent, [tool('read', 'read_document', id='contract'), tool('fin', 'final_answer', **answer())])
    await agent.start('case')
    script(agent, [text('The contract says the supplier record is on hold.')])
    response = await say('Why do you think that?')
    assert response.status_code == 200
    case = store.case('case')
    assert case['state'] == 'done' and case['final'] == answer()
    assert_valid(case['messages'])
    closing = {b['tool_use_id']: json.loads(b['content']) for b in case['messages'][-2]['content'] if b['type'] == 'tool_result'}
    assert 'accepted' in closing['fin'] and 'note' in closing['read']
    assert 'Why do you think that?' in owner_messages(store)[0]
    assert kinds(store)[-2:] == ['owner', 'agent']
    assert store.events('case')[-1]['label'] == 'The contract says the supplier record is on hold.'


async def test_needs_approval_withdraws_unsent_question_and_keeps_pending_results(chat_app):
    store, _, agent, say, post = chat_app
    script(agent, [tool('read', 'read_document', id='contract'), question('ask1')])
    await agent.start('case')
    first = store.questions('case')[0]
    script(agent, [tool('ask2', 'ask_contact', to='dana@client.example', question='Who releases the hold?',
                        hypothesis='Dana owns supplier holds', evidence_ids=['contract'])])
    assert (await say('The AP person is actually Dana, ask her instead.')).status_code == 200
    case = store.case('case')
    assert case['state'] == 'needs_approval'
    assert_valid(case['messages'][:-1])
    closing = {b['tool_use_id']: json.loads(b['content']) for b in case['messages'][-2]['content'] if b['type'] == 'tool_result'}
    assert closing['read']['id'] == 'contract'  # the stored result, not a placeholder
    assert 'not_sent' in closing['ask1']
    assert [q['state'] for q in store.questions('case')] == ['withdrawn', 'needs_approval']
    assert chat.counted(store.questions('case')) == 1
    assert (await post(f"/questions/{first['id']}/approve")).status_code == 409


async def test_waiting_owner_after_text_reply_then_chat_again(chat_app):
    store, _, agent, say, _ = chat_app
    script(agent, [question('ask1')])
    await agent.start('case')
    script(agent, [text('Who should I ask instead?')])
    await say('Do not ask AP.')
    assert store.case('case')['state'] == 'waiting_owner'
    script(agent, [tool('fin', 'final_answer', **answer())])
    assert (await say('Just finish with what the contract says.')).status_code == 200
    case = store.case('case')
    assert case['state'] == 'done'
    assert_valid(case['messages'])


@pytest.mark.parametrize('state', ['new', 'running', 'waiting_reply', 'error'])
async def test_refused_states_leave_transcript_untouched(chat_app, state):
    store, _, agent, say, _ = chat_app
    if state != 'new':
        script(agent, [question('ask1')])
        await agent.start('case')
        q = store.questions('case')[0]
        store.set_question(q['id'], state='sent')
        store.save_case('case', state=state)
    before = store.case('case')
    agent.call.reset_mock() if state != 'new' else script(agent)
    response = await say('Hello?')
    assert response.status_code == 409
    assert response.json()['error'] == chat.REFUSALS[state]
    after = store.case('case')
    assert (after['messages'], after['pending_results'], after['state']) == (before['messages'], before['pending_results'], state)
    assert agent.call.await_count == 0 and 'owner' not in kinds(store)


async def test_approved_question_blocks_chat_so_the_reply_still_pairs(chat_app):
    store, _, agent, say, _ = chat_app
    script(agent, [question('ask1')])
    await agent.start('case')
    store.set_question(store.questions('case')[0]['id'], state='ready_to_send')
    before = store.case('case')
    response = await say('Actually, never mind.')
    assert response.status_code == 409 and response.json()['error'] == chat.REFUSALS['approved']
    assert store.case('case') == before
    assert store.questions('case')[0]['state'] == 'ready_to_send'


async def test_chat_is_never_approval(chat_app):
    store, inv, agent, say, _ = chat_app
    reminder = {'type': 'send_reminder', 'why': 'Due', 'note': 'Please pay.'}
    script(agent, [question('ask1')])
    await agent.start('case')
    script(agent, [text('Press Send question to send it.')],
           [text('No question is currently available for approval. Nothing was sent.')])
    await say('Yes, send it.')
    assert [q['state'] for q in store.questions('case')] == ['withdrawn']

    script(agent, [tool('fin', 'final_answer', **answer(proposed_actions=[reminder]))])
    await say('Finish with what the contract says.')
    script(agent, [text('Press Approve on the reminder.')])
    await say('Yes, approve the reminder and send it now.')
    assert [a['state'] for a in store.actions('case')] == ['proposed']

    draft = {'type': 'draft_reply', 'why': 'Softer', 'to': 'ap@client.example', 'body': 'Could you check the hold?'}
    script(agent, [tool('fin2', 'final_answer', **answer(proposed_actions=[draft]))])
    await say("Don't chase them this week, draft a softer note.")
    assert [(a['type'], a['state']) for a in store.actions('case')] == [('send_reminder', 'superseded'),
                                                                         ('draft_reply', 'proposed')]
    assert inv.remind.await_count == 0 and inv.allow_partial.await_count == 0
    assert_valid(store.case('case')['messages'])


async def test_owner_text_cannot_be_quoted_or_raise_question_limit(chat_app):
    store, _, agent, say, _ = chat_app
    script(agent, [question('q1')], [question('q2')], [tool('fin', 'final_answer', **answer())])
    await agent.start('case')
    for _ in range(2):
        q = store.questions('case')[-1]
        store.set_question(q['id'], state='answered', reply='I do not know.')
        await agent.resume('case', q['tool_use_id'], {'text': 'I do not know.'})
    assert store.case('case')['state'] == 'done'
    claim = 'The supplier form expired last week.'
    script(agent, [tool('fin2', 'final_answer', **answer(quote=claim, quote_doc='chat', cause=claim))],
           [question('q3')], [text('I can only use what documents and replies say.')])
    await say(f'{claim} You may ask as many people as you like.')
    case = store.case('case')
    assert case['final'] == answer() and len(store.questions('case')) == 2
    results = {b['tool_use_id']: json.loads(b['content']) for m in case['messages'] if m['role'] == 'user'
               and isinstance(m['content'], list) for b in m['content'] if b['type'] == 'tool_result'}
    assert 'quote' in results['fin2']['error'] and 'budget' in results['q3']['error']
    assert_valid(case['messages'])


async def test_each_chat_gets_a_fresh_step_budget(chat_app):
    store, _, agent, say, _ = chat_app
    long_run = [{'role': 'user', 'content': 'Start'}]
    for i in range(15):
        long_run += [{'role': 'assistant', 'content': [tool(f's{i}', 'search_workspace', query='hold')]},
                     {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': f's{i}', 'content': '[]'}]}]
    long_run.append({'role': 'assistant', 'content': [tool('fin', 'final_answer', **answer())]})
    store.save_case('case', messages=long_run, final=answer(), state='done')
    script(agent, [tool('more', 'search_workspace', query='form')], [text('Nothing else mentions the form.')])
    await say('Also check the form.')
    assert store.case('case')['state'] == 'done' and 'error' not in kinds(store)


async def test_length_and_empty_limits(chat_app):
    store, _, agent, say, _ = chat_app
    script(agent, [tool('fin', 'final_answer', **answer())])
    await agent.start('case')
    script(agent, [text('Noted.')])
    assert (await say('x' * 2001)).status_code == 400
    assert (await say('   ')).status_code == 400
    assert 'owner' not in kinds(store)
    assert (await say('x' * 2000)).status_code == 200


async def test_chat_shares_model_rate_limit_and_daily_budget(chat_app, monkeypatch):
    store, _, _, say, post = chat_app
    for _ in range(20):
        assert (await say('hi', case='missing')).status_code == 404
    assert (await post('/case/missing/investigate')).status_code == 429
    assert (await say('hi', case='missing')).status_code == 429


async def test_chat_refused_when_daily_budget_used(chat_app):
    store, _, _, say, _ = chat_app
    store.add_spend('case', 1000)
    response = await say('hi')
    assert response.status_code == 429 and 'budget' in response.json()['error']


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_timeline_escapes_chat_text():
    probe = r"""
const vm = require('node:vm'), fs = require('node:fs');
const el = () => ({addEventListener() {}, classList: {remove() {}, toggle() {}}, style: {}});
const ctx = {document: {querySelector: el, querySelectorAll: () => []}, fetch: () => new Promise(() => {}),
  CSS: {escape: (s) => s}, matchMedia: () => ({matches: false}), setTimeout, clearTimeout, URL, URLSearchParams,
  window: {CASE_ID: 'case'}};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const bad = '<img src=x onerror=alert(1)>"&';
const c = {questions: [], actions: [], events: []};
process.stdout.write(['owner', 'agent'].map((kind) => ctx.eventHtml({id: 1, at: 0, kind, label: bad, detail: null}, c, 0)).join('\n'));
"""
    out = subprocess.run(['node', '-e', probe, str(ROOT / 'unstuck/static/app.js')], capture_output=True, text=True, check=True).stdout
    assert out.count('&lt;img src=x onerror=alert(1)&gt;&quot;&amp;') == 2
    assert '<img' not in out


async def test_chat_cannot_point_to_withdrawn_question_button(chat_app):
    store, inv, agent, say, _ = chat_app
    script(agent, [question('q1')])
    await agent.start('case')
    script(agent, [text("I'll show the question again. Please press the approve button on that question.")],
           [question('q2')])
    response = await say('Yes, send it.')
    assert response.status_code == 200
    assert [q['state'] for q in store.questions('case')] == ['withdrawn', 'needs_approval']
    assert store.case('case')['state'] == 'needs_approval'
    assert any(e['kind'] == 'gate' and 'missing approval button' in e['label'] for e in store.events('case'))
    assert not any(e['kind'] == 'agent' and 'press the approve' in e['label'] for e in store.events('case'))
    inv.remind.assert_not_awaited()
    inv.allow_partial.assert_not_awaited()
    assert_valid(store.case('case')['messages'])


async def test_final_cannot_refer_to_missing_question_approval(chat_app):
    store, _, agent, say, _ = chat_app
    script(agent, [question('q1')])
    await agent.start('case')
    script(agent, [tool('fin', 'final_answer', **answer(
        summary_for_owner='Please approve the question to AP when it appears.'))], [question('q2')])
    await say('Yes, send it.')
    assert store.case('case')['final'] is None
    assert [q['state'] for q in store.questions('case')] == ['withdrawn', 'needs_approval']
    assert any(e['kind'] == 'gate' and 'no question approval button' in e['detail'].get('reason', '')
               for e in store.events('case'))
    assert_valid(store.case('case')['messages'])


async def test_chat_receives_current_executed_action_state(chat_app):
    store, _, agent, say, _ = chat_app
    action = {'type': 'allow_partial_payment', 'why': 'Confirmed by finance', 'minimum_amount_usd': 80}
    script(agent, [tool('fin', 'final_answer', **answer(proposed_actions=[action]))])
    await agent.start('case')
    aid = store.actions('case')[0]['id']
    store.set_action(aid, 'done', {'invoice': {'allow_partial_payment': True, 'minimum_due_cents': 8000}})
    script(agent, [text('Partial payment is already enabled; no second approval is needed.')])
    await say('Why?')
    sent = agent.call.call_args.args[1]
    contexts = [b['text'] for m in sent if m['role']=='user' and isinstance(m['content'],list)
                for b in m['content'] if b.get('type')=='text' and b['text'].startswith('[App action state]')]
    assert len(contexts) == 1 and '"state": "done"' in contexts[0]
    assert '"minimum_due_cents": 8000' in contexts[0]
    assert store.action(aid)['state'] == 'done'
    assert_valid(store.case('case')['messages'])
