"""Start real uvicorn processes and exercise HTTP forms with synthetic credentials only.

Run: uv run --python 3.13 python tests/http_smoke.py
No provider network: dummy mode is forced and inherited provider keys are removed.
"""
import base64
import hashlib
import hmac
import os
import re
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import httpx

ROOT = Path(__file__).resolve().parent.parent
BANNER = 'Dummy PayPal, not Sandbox'


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms=[]
        self.current=None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='form':
            self.current={'action':attrs['action'],'data':{}}
            self.forms.append(self.current)
        if tag=='input' and self.current is not None and attrs.get('name'):
            self.current['data'][attrs['name']]=attrs.get('value','')

    def handle_endtag(self,tag):
        if tag=='form': self.current=None


def create_fault_app():
    """Test-only app factory: same main wiring with one deterministic lost response."""
    from daybreak import main
    from daybreak.dummy_paypal import DummyPayPal
    from daybreak.paypal import PayPalClient
    dummy=DummyPayPal()
    for _,capture,cents,_ in main.ORDERS: dummy.add_capture(capture,cents)
    dummy.faults=['lost']
    original=main._paypal
    main._paypal=lambda log:(PayPalClient('synthetic','synthetic',transport=dummy.transport(),log=log),'dummy')
    try: return main.create_app()
    finally: main._paypal=original


def signed_link(order, secret):
    payload=f'order:{order}.{int(time.time())+3600}'
    signature=base64.urlsafe_b64encode(hmac.new(secret.encode(),payload.encode(),hashlib.sha256).digest()).decode().rstrip('=')
    return '/order/'+payload+'.'+signature


def get(client,path):
    response=client.get(path)
    assert response.status_code==200,(path,response.status_code)
    assert BANNER in response.text
    return response


def submit(client,response,suffix,**values):
    form=next(f for f in Forms(response.text).forms if f['action'].endswith(suffix)
              and (suffix != '/choose' or f['data'].get('option_id') == 'refund'))
    result=client.post(form['action'],data={**form['data'],**values})
    assert result.status_code in (200,303),(suffix,result.status_code)
    if result.status_code==303:
        return get(client,result.headers['location'])
    assert BANNER in result.text
    return result


def run(fault=False):
    checks=ROOT/'.checks';checks.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='http-',dir=checks) as folder:
        folder=Path(folder)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        secret=secrets.token_urlsafe(40)
        # The subprocess cannot select real providers even if the calling shell has credentials.
        env={k:v for k,v in os.environ.items() if k not in {'PAYPAL_CLIENT_ID','PAYPAL_CLIENT_SECRET','ANTHROPIC_API_KEY','DAYBREAK_RESTORED'}}
        env.update(PYTHONPATH=str(ROOT), DAYBREAK_DUMMY_PAYPAL='1',DAYBREAK_SECRET=secret,
                   DAYBREAK_SELLER_PASSWORD='synthetic-http-password',
                   DAYBREAK_DB=str(folder/'app.db'),DAYBREAK_LOG=str(folder/'requests.jsonl'))
        if not fault: env['DAYBREAK_RESTORED']='1'
        command=['uv','run','--python','3.13','uvicorn',
                 'http_smoke:create_fault_app' if fault else 'daybreak.main:create_app',
                 '--factory','--host','127.0.0.1','--port',str(port),'--no-access-log']
        if fault: command+=['--app-dir','tests']
        with (folder/'server.log').open('w') as log:
            process=subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=log)
            try:
                with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=5) as client:
                    for _ in range(100):
                        if process.poll() is not None: raise AssertionError('Server exited: '+(folder/'server.log').read_text())
                        try:
                            response=client.get('/health')
                            if response.status_code==200: break
                        except httpx.ConnectError: pass
                        time.sleep(.1)
                    else: raise AssertionError('Server did not start')
                    assert response.json()['paypal_mode']=='dummy'
                    assert BANNER in client.get('/seller').text
                    dashboard=submit(client,get(client,'/login'),'/login',password='synthetic-http-password')
                    assert client.cookies.get('daybreak_seller')
                    if not fault:
                        assert 'Automatic execution is paused' in dashboard.text
                        dashboard=submit(client,dashboard,'/seller/resume',reviewed='yes')
                        assert 'Automatic execution is paused' not in dashboard.text
                    expires=(datetime.now(timezone.utc)+timedelta(hours=7)).isoformat()
                    dashboard=submit(client,dashboard,'/seller/delegations',per_refund='50',total='100',expires_at=expires)
                    assert 'Latest delegation' in dashboard.text
                    reviews={}
                    def request(order):
                        if order not in reviews:
                            page=get(client,signed_link(order,secret))
                            reviews[order]=submit(client,page,'/message',message='Please refund me')
                        return submit(client,reviews[order],'/choose')
                    first=request('order-39')
                    if fault:
                        assert 'Result unconfirmed' in first.text
                        overview=get(client,'/seller')
                        path=re.search(r'href="(/seller/cases/[^"]+)"',overview.text).group(1)
                        detail=get(client,path)
                        done=submit(client,detail,'/reconcile')
                        assert 'completed' in done.text
                        assert 'Result unconfirmed' not in done.text
                    else:
                        assert 'completed' in first.text
                        assert 'completed' in request('order-49').text
                        assert 'The seller will review your request' in request('order-29').text
                        duplicate=request('order-39')
                        assert 'Refund completed' in duplicate.text
                        assert re.search(r'DUMMY-[A-Z0-9]+', first.text).group(0) in duplicate.text
                        overview=get(client,'/seller')
                        assert '$88.00' in overview.text and '$29.00' in overview.text and '$12.00' in overview.text
                        paths=re.findall(r'href="(/seller/cases/[^"]+)"',overview.text)
                        for path in paths:
                            detail=get(client,path)
                            if any(f['action'].endswith('/approve') for f in Forms(detail.text).forms):
                                done=submit(client,detail,'/approve');assert 'completed' in done.text
                                break
                        else: raise AssertionError('No approval form')
                        overview=get(client,'/seller')
                        assert '$88.00' in overview.text and '$29.00' in overview.text and '$12.00' in overview.text
                    dashboard=submit(client,get(client,'/seller'),'/delegations/stop')
                    assert 'Not stopped' not in dashboard.text
                    submit(client,dashboard,'/logout')
                    assert client.get('/seller').status_code==401
            finally:
                process.terminate()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait()
    return 'lost-response reconciliation' if fault else '39 + 49 automatic, 29 approval, duplicate, stop and logout'


if __name__=='__main__':
    for fault in (False,True):
        print('PASS — Dummy PayPal, not Sandbox: '+run(fault))
