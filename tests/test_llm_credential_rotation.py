"""Transport credentials stay redacted after environment selectors rotate."""
import json

import httpx
import pytest

from backend.models import ArkClient
from backend.services.contracts import LLMRequest, ProviderError
from backend.services.llm import ChatProvider, CompatibleTransport


@pytest.mark.parametrize('provider_name', ['ark', 'openai_compatible'])
@pytest.mark.parametrize('mode', ['complete', 'stream', 'error'])
async def test_initial_and_current_credentials_are_redacted_before_truncation(monkeypatch, provider_name, mode):
    original = 'synthetic-original-' + 'x' * 350
    replacement = 'synthetic-replacement-' + 'y' * 350
    monkeypatch.setenv('OFFLINE_ROTATING_MODEL_KEY', original)
    profile = {'provider': provider_name, 'key_env': 'OFFLINE_ROTATING_MODEL_KEY',
               'base_url': 'https://offline.test/'}
    seen = []

    def respond(request):
        seen.append(request.headers['authorization'])
        if mode == 'error':
            # This used to leak a truncated prefix before the outer layer could
            # replace the full initial token, including in the attempt summary.
            raise ValueError(original + ' malformed provider response')
        content = original + ' ' + replacement
        if mode == 'complete':
            return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': content}}]})
        return httpx.Response(200, text='data: ' + json.dumps({
            'choices': [{'delta': {'content': content}, 'finish_reason': 'stop'}]}) + '\n\ndata: [DONE]\n\n')

    client = httpx.AsyncClient(base_url=profile['base_url'], headers={'Authorization': 'Bearer ' + original},
                              transport=httpx.MockTransport(respond))
    transport = (ArkClient(client, key_env=profile['key_env']) if provider_name == 'ark'
                 else CompatibleTransport(profile, client))
    provider = ChatProvider(profile, transport=transport)
    monkeypatch.setenv(profile['key_env'], replacement)
    try:
        request = LLMRequest([], model='offline-model')
        if mode == 'error':
            with pytest.raises(ProviderError) as failure:
                _ = [item async for item in provider.stream(request)]
            recorded = json.dumps({'public': failure.value.public(), 'details': failure.value.details,
                                   'partial': failure.value.trace_partial})
        elif mode == 'complete':
            result = await provider.complete(request)
            recorded = json.dumps({'message': result.message, 'meta': result.metadata})
        else:
            recorded = json.dumps([item async for item in provider.stream(request)])
        assert seen == ['Bearer ' + original]
        assert original[:100] not in recorded and replacement[:100] not in recorded
        assert '[redacted]' in recorded
        assert {original, replacement} <= set(provider.secrets)
    finally:
        await provider.aclose()
