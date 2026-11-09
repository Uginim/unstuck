from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from daybreak.extract import AnthropicExtractor, DummyExtractor, build_extractor, validate_output
from daybreak.logbook import Logbook

@pytest.mark.parametrize('value', [None, {}, {'kind':'refund','ambiguous':0,'summary':'x'},
    {'kind':'approve','ambiguous':False,'summary':'x'},
    {'kind':'refund','ambiguous':False,'summary':''},
    {'kind':'refund','ambiguous':False,'summary':'x','amount':3900}])
def test_invalid_output_rejected(value):
    with pytest.raises(ValueError): validate_output(value)

async def test_structured_call_and_validation():
    response = SimpleNamespace(content=[SimpleNamespace(type='text', text='{"kind":"refund","ambiguous":false,"summary":"Refund requested."}')], stop_reason='end_turn', usage=Mock())
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))
    extractor = AnthropicExtractor(client, 'test-model', Logbook(None))
    assert (await extractor.extract('Refund please', 'Workshop')).kind == 'refund'
    assert client.messages.create.call_args.kwargs['output_config']['format']['schema']['additionalProperties'] is False
    response.stop_reason = 'max_tokens'
    with pytest.raises(ValueError): await extractor.extract('Refund please', 'Workshop')

def test_mode_selection(monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.delenv('DAYBREAK_DUMMY_PAYPAL', raising=False)
    with pytest.raises(RuntimeError): build_extractor(Logbook(None))
    monkeypatch.setenv('DAYBREAK_DUMMY_PAYPAL', '1')
    assert isinstance(build_extractor(Logbook(None)), DummyExtractor)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'synthetic-test-only')
    client = Mock()
    monkeypatch.setattr('daybreak.extract.AsyncAnthropic', lambda **kw: client)
    assert build_extractor(Logbook(None)).model == 'claude-sonnet-5-5'
    monkeypatch.setenv('ANTHROPIC_MODEL', 'test-override')
    assert build_extractor(Logbook(None)).model == 'test-override'

async def test_dummy_is_explicit_and_conservative():
    extractor = DummyExtractor()
    assert (await extractor.extract('Please refund me', 'Workshop')).kind == 'refund'
    assert (await extractor.extract('What is the refund policy?', 'Workshop')).kind == 'question'
    assert (await extractor.extract('Unrecognized text', 'Workshop')).ambiguous
