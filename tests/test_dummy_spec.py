"""Independent checks from the public Payments v2 schema, never live API calls.
Source: https://developer.paypal.com/api/payments/v2/schema.yaml
"""
import httpx
import pytest
from daybreak.dummy_paypal import DummyPayPal
from daybreak.paypal import PayPalClient


async def test_error_envelope_required_fields():
    dummy=DummyPayPal()
    dummy.add_capture('synthetic-capture',3900)
    dummy.faults=['REFUND_NOT_ALLOWED']
    async with httpx.AsyncClient(transport=dummy.transport(),base_url='https://dummy.invalid') as client:
        response=await client.post('/v2/payments/captures/synthetic-capture/refund',
            headers={'PayPal-Request-Id':'synthetic-key'},json={'amount':{'value':'39.00','currency_code':'USD'}})
    body=response.json()
    assert response.status_code==422
    assert {'name','message','debug_id'} <= body.keys(), 'components.schemas.error required fields'
    assert body['name']=='UNPROCESSABLE_ENTITY'
    assert body['details'][0]['issue']=='REFUND_NOT_ALLOWED'


@pytest.mark.parametrize('fault,expected',[
    ('REFUND_NOT_ALLOWED','failed_definite'),
    ('REFUND_FAILED_INSUFFICIENT_FUNDS','failed_definite'),
    ('CAPTURE_FULLY_REFUNDED','unknown'),
    ('DUPLICATE_REFUND','unknown'),
    ('PREVIOUS_REQUEST_IN_PROGRESS','unknown'),
    ('500','unknown'),('timeout','unknown'),('lost','unknown'),('pending','pending'),
])
async def test_known_error_and_status_mapping(fault,expected):
    dummy=DummyPayPal();dummy.add_capture('synthetic-capture',3900);dummy.faults=[fault]
    client=PayPalClient('synthetic-id','synthetic-secret',transport=dummy.transport())
    try:
        result=await client.refund('synthetic-capture',3900,'USD','synthetic-key')
        assert result.status==expected
        assert dummy.refund_calls==1
    finally: await client.aclose()


async def test_currency_mismatch_uses_documented_issue():
    dummy=DummyPayPal();dummy.add_capture('synthetic-capture',3900)
    async with httpx.AsyncClient(transport=dummy.transport(),base_url='https://dummy.invalid') as client:
        response=await client.post('/v2/payments/captures/synthetic-capture/refund',
            headers={'PayPal-Request-Id':'synthetic-key'},json={'amount':{'value':'39.00','currency_code':'EUR'}})
    assert response.status_code==422
    # Both aliases are explicitly listed in the official schema's error catalog.
    assert response.json()['details'][0]['issue'] in {'REFUND_CAPTURE_CURRENCY_MISMATCH', 'REFUND_CURRENCY_MISMATCH'}
