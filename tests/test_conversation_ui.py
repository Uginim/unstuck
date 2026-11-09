from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from daybreak.ports import ConversationView, Option, TimelineEvent, AgentTurn
from test_web import web, csrf, seller, token
from test_integration import NOW


async def conversation(client, message='Please refund me', **fields):
    link='/order/'+token()
    await client.get(link)
    response=await client.post(link+'/message',data={'csrf':csrf(client),'message':message,**fields})
    assert response.status_code==200
    return link,response


async def test_ko_options_only_from_contract_and_suggestion(web,monkeypatch):
    c,w=web
    view=ConversationView('draft',7,'선택지를 확인해 주세요.',True,'ko-KR',[
        Option('keep','keep','Untrusted-to-the-view label $999'),
        Option('move:s1','move','Move',starts_at=NOW+timedelta(days=3)),
    ],'move:s1','Synthetic workshop', [('customer','일정을 바꾸고 싶어요'),('agent','선택지를 확인해 주세요.')])
    call=AsyncMock(return_value=view)
    monkeypatch.setattr(w.service,'converse',call)
    link,response=await conversation(c,'일정을 바꾸고 싶어요')
    assert '<html lang="ko">' in response.text
    assert 'AI 답변' in response.text and '답변을 교체했습니다' in response.text
    assert '일정 변경 확정' in response.text and '예약 유지' in response.text
    assert '환불 요청 확정' not in response.text
    assert 'option suggested' in response.text and 'AI 제안' in response.text
    assert 'value="move:s1"' in response.text and 'value="refund"' not in response.text
    assert '2026-11-11 01:00 UTC' in response.text
    assert '$999' not in response.text
    call.assert_awaited_once_with('a','일정을 바꾸고 싶어요',None)


async def test_conversation_continues_and_choice_rejects_stale_or_forged(web):
    c,w=web
    link,response=await conversation(c)
    draft,version=w.sql('SELECT id,version FROM request_drafts')[0]
    assert 'value="refund"' in response.text
    assert w.adapter_calls==0
    response=await c.post(link+'/message',data={'csrf':csrf(c),'message':'Another question','draft_id':draft})
    assert response.status_code==200
    assert response.text.count('AI reply')==2
    response=await c.post(link+'/choose',data={'csrf':csrf(c),'draft_id':draft,'draft_version':version,'option_id':'refund'})
    assert response.status_code==409
    response=await c.post(link+'/choose',data={'csrf':csrf(c),'draft_id':draft,'draft_version':version+1,'option_id':'move:forged'})
    assert response.status_code==409
    assert w.adapter_calls==0


@pytest.mark.parametrize('kind',['keep','move'])
async def test_move_and_keep_display_no_refund_and_timeline(web,kind):
    c,w=web
    if kind=='move':
        w.sql('INSERT INTO sessions VALUES (?,?,?,?)',('s1','Synthetic workshop a',(NOW+timedelta(days=10)).isoformat(),1))
    link,response=await conversation(c)
    draft,version=w.sql('SELECT id,version FROM request_drafts')[0]
    response=await c.post(link+'/choose',data={'csrf':csrf(c),'draft_id':draft,'draft_version':version,
        'option_id':'keep' if kind=='keep' else 'move:s1','language':'ko'})
    assert response.status_code==200
    assert ('예약 유지 완료' if kind=='keep' else '예약 일정 변경 완료') in response.text
    assert '환불은 실행되지 않았습니다.' in response.text
    assert 'Seller allowed' not in response.text and 'Delegation' not in response.text
    assert w.adapter_calls==0
    seller(c)
    dashboard=await c.get('/seller')
    assert ('Booking kept' if kind=='keep' else 'Booking moved') in dashboard.text
    assert '<ol id="timeline-list"' in dashboard.text
    assert 'No refund sent.' in dashboard.text


async def test_refund_choice_is_service_confirmed_and_private(web):
    c,w=web
    await w.delegate()
    link,response=await conversation(c)
    draft,version=w.sql('SELECT id,version FROM request_drafts')[0]
    response=await c.post(link+'/choose',data={'csrf':csrf(c),'draft_id':draft,'draft_version':version,'option_id':'refund'})
    assert response.status_code==200 and 'Refund completed' in response.text
    assert '$39.00' in response.text and w.adapter_calls==1
    for value in ('Seller allowed','Delegation','$50.00','$100.00','Reason:'):
        assert value not in response.text


async def test_timeline_sorted_escaped_and_progressive(web,monkeypatch):
    c,w=web;seller(c)
    monkeypatch.setattr(w.service,'timeline',lambda:[
        TimelineEvent(NOW+timedelta(seconds=2),'r2','kept','Second <script>bad</script>'),
        TimelineEvent(NOW,'r1','moved','First event'),
    ])
    response=await c.get('/seller')
    assert response.text.index('First event')<response.text.index('Second &lt;script&gt;')
    assert '<script>bad</script>' not in response.text
    assert '<li><time' in response.text  # list is present before JS executes
    assert 'src="/static/timeline.js"' in response.text
    assert 'unsafe-inline' not in response.headers['content-security-policy']
    assert (await c.get('/static/timeline.js')).status_code==200


@pytest.mark.parametrize('mode,turns,conversations',[('dummy',0,0),('dummy',4,2),('sandbox',0,0),('sandbox',4,2),('sandbox',1,0)])
async def test_ai_cost_measured_only_and_seller_only(web,monkeypatch,mode,turns,conversations):
    from daybreak.ports import AiUsage
    c,w=web
    c._transport.app.state.paypal_mode=mode  # label only; payments still use the injected dummy transport
    monkeypatch.setattr(w.service,'ai_usage',lambda:AiUsage('synthetic-model',conversations,turns,1200,300,123456))
    seller(c)
    response=await c.get('/seller')
    assert response.status_code==200
    panel=response.text.split('aria-labelledby="ai-cost-title"',1)[1].split('</section>',1)[0]
    assert 'AI cost' in panel and '1200' in panel and '300' in panel and 'synthetic-model' in panel
    assert ('No measured model calls' in panel)==(turns==0)
    if mode=='dummy' or turns==0:
        assert '$' not in panel and 'Measured total cost' not in panel
    else:
        assert '$0.123456' in panel
        assert ('$0.061728' in panel) if conversations else ('Not available' in panel)
    customer=await c.get('/order/'+token())
    assert 'AI cost' not in customer.text and 'synthetic-model' not in customer.text


@pytest.mark.parametrize('language',['en','ko'])
async def test_move_deadline_notice_and_new_date(web,monkeypatch,language):
    c,w=web
    starts=NOW+timedelta(days=10)
    w.sql('INSERT INTO sessions VALUES (?,?,?,?)',('ui2-session','Synthetic workshop a',starts.isoformat(),1))
    async def respond(*args):
        return AgentTurn('other',False,'Change booking','가능한 일정을 선택하세요.' if language=='ko' else 'Choose a date.',language,'move:ui2-session')
    monkeypatch.setattr(w.service._agent,'respond',respond)
    link,response=await conversation(c)
    notice=('옮겨도 환불 기한은 원래 회차 기준입니다.' if language=='ko' else
            'Moving your booking does not change the refund deadline: it stays based on the original session.')
    assert notice in response.text
    assert 'aria-describedby="move-rule-' in response.text
    if language=='ko':
        for english in ('Your workshop request','Plans changed?','AI reply','AI replies are conversational guidance',
                        'Anyone holding your valid order link','Seller sign in','Conversation responses in local tests'):
            assert english not in response.text
    draft,version=w.sql('SELECT id,version FROM request_drafts')[0]
    result=await c.post(link+'/choose',data={'csrf':csrf(c),'draft_id':draft,'draft_version':version,
        'option_id':'move:ui2-session','language':language})
    assert result.status_code==200
    assert f'datetime="{starts.isoformat()}"' in result.text
    assert '2026-11-18 01:00 UTC' in result.text
    assert ('변경된 워크숍 일시' if language=='ko' else 'New workshop date') in result.text
    if language=='ko':
        for english in ('Workshop order','Send another message','Plans changed?'):
            assert english not in result.text
    assert w.adapter_calls==0


async def test_seller_sections_and_pending_cases_are_first(web):
    c,w=web
    await w.delegate(per=0)
    pending=await w.submit()
    w.sql('UPDATE delegations SET per_refund_limit_cents=5000')
    w.order('later',2900)
    done=await w.submit('later')
    seller(c)
    html=(await c.get('/seller')).text
    anchors=['aria-label="Refund totals"','<h2>Requests</h2>','<h2>Automatic permission</h2>',
             '<h2 id="timeline-title">','<h2 id="ai-cost-title">']
    positions=[html.index(anchor) for anchor in anchors]
    assert positions==sorted(positions)
    requests=html.split('<h2>Requests</h2>')[1].split('<aside')[0]
    assert requests.index(pending.request_id)<requests.index(done.request_id)
    assert '>Refund authorized</a>' in html
    assert '>execute</a>' not in html


@pytest.mark.parametrize('duration',['1','4','8','12','24'])
async def test_delegation_duration_is_calculated_on_server(web,monkeypatch,duration):
    from datetime import datetime
    c,w=web
    class FixedDatetime(datetime):
        @classmethod
        def now(cls,tz=None): return NOW
    monkeypatch.setattr('daybreak.web.datetime',FixedDatetime)
    seller(c)
    page=await c.get('/seller')
    assert 'name="duration_hours"' in page.text and 'name="expires_at"' not in page.text
    response=await c.post('/seller/delegations',data={'csrf':csrf(c),'per_refund':'50','total':'100',
        'duration_hours':duration,'expires_at':'2099-01-01T00:00:00Z'})
    assert response.status_code==303
    assert w.service.latest_delegation().expires_at==NOW+timedelta(hours=int(duration))


@pytest.mark.parametrize('duration',['0','-1','999','1.5','tomorrow',''])
async def test_invalid_duration_does_not_start_delegation(web,duration):
    c,w=web;seller(c);await c.get('/seller')
    response=await c.post('/seller/delegations',data={'csrf':csrf(c),'per_refund':'50','total':'100','duration_hours':duration})
    assert response.status_code==422
    assert w.service.latest_delegation() is None
