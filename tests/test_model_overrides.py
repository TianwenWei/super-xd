"""Explicit HTTP doubles verify role-specific model selection on the wire."""
import json

import httpx
import pytest

from backend.models import ArkClient
from backend.settings import TALKER_MODEL, THINKER_MODEL, VEHICLE_CONTROL_MODEL


async def test_vehicle_pro_override_does_not_change_talker_or_research(monkeypatch):
    monkeypatch.setenv('ARK_API_KEY', 'fake-transport-only-credential')
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={
            'id': 'transport-observation', 'model': body['model'],
            'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': []},
                         'finish_reason': 'stop'}],
        })

    async with httpx.AsyncClient(base_url='https://provider.test/',
                                transport=httpx.MockTransport(respond)) as client:
        ark = ArkClient(client)
        messages = [{'role': 'user', 'content': '测试角色隔离'}]
        pro = await ark.complete(messages, model=VEHICLE_CONTROL_MODEL, thinking=False, max_tokens=2000)
        await ark.complete(messages)
        await ark.complete(messages, thinker=True)
    assert [body['model'] for body in requests] == [VEHICLE_CONTROL_MODEL, TALKER_MODEL, THINKER_MODEL]
    assert [body['thinking']['type'] for body in requests] == ['disabled', 'disabled', 'enabled']
    assert [body['max_tokens'] for body in requests] == [2000, 900, 6000]
    assert pro['_meta']['model'] == VEHICLE_CONTROL_MODEL
    assert pro['_meta']['raw_response']['model'] == VEHICLE_CONTROL_MODEL


@pytest.mark.parametrize('options', [
    {'model': ''}, {'model': ' \t'}, {'model': 42},
    {'thinking': 'disabled'}, {'thinking': 0},
])
async def test_invalid_role_override_never_sends_request(monkeypatch, options):
    monkeypatch.setenv('ARK_API_KEY', 'fake-transport-only-credential')
    def unexpected(request):
        pytest.fail('Invalid role override must be rejected before HTTP')
    async with httpx.AsyncClient(base_url='https://provider.test/',
                                transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(ValueError):
            ArkClient(client).payload([], None, **options)
