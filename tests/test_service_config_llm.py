"""Provider routing must not change native messages or lose streaming evidence."""
from copy import deepcopy
import json
import tomllib

import httpx
import pytest

from backend.services.config import ServiceConfig
from backend.services.contracts import LLMRequest, ProviderError, ToolCapability
from backend.services.llm import ChatProvider, CompatibleTransport, LLMService
from backend.settings import ROOT


def document():
    with (ROOT / 'config/services.toml').open('rb') as source:
        return tomllib.load(source)


@pytest.mark.parametrize('domain,field,value', [
    ('asr', 'provider', 'imaginary'), ('tts', 'extra', True),
    ('embedding', 'dimensions', 0), ('llm', 'timeout_seconds', False),
])
def test_unknown_or_mistyped_service_configuration_fails_at_startup(domain, field, value):
    config = document()
    profile = next(iter(config[domain]['profiles'].values()))
    profile[field] = value
    with pytest.raises(ValueError):
        ServiceConfig(config)


def test_unknown_selected_provider_never_falls_back(monkeypatch):
    monkeypatch.setenv('ASR_PROFILE', 'missing')
    with pytest.raises(ValueError):
        ServiceConfig(document())


@pytest.mark.parametrize('operation,location', [('query', 'device'), ('query', 'cloud'), ('control', 'device')])
def test_tool_operation_is_independent_of_execution_location(operation, location):
    assert ToolCapability('search', operation, location, 'media').to_dict()['execution_location'] == location


async def test_role_configuration_controls_model_without_rewriting_message_content(monkeypatch):
    config = document()
    config['llm']['roles']['planner'].pop('model_env')
    config['llm']['roles']['planner']['model'] = 'configured-planner'
    service = LLMService(ServiceConfig(config))
    try:
        messages = [{'role': 'user', 'content': '{{unaltered user data}}'}]
        request = service.for_role('planner').request(messages, model='legacy-model-hint')
        assert request.model == 'configured-planner'
        assert request.role == 'planner' and request.reasoning is False and request.max_tokens == 2000
        assert request.messages is messages
    finally:
        await service.aclose()


async def test_role_limits_cannot_be_bypassed_by_legacy_call_hints():
    config = document()
    config['llm']['roles']['planner']['max_tokens'] = 500
    service = LLMService(ServiceConfig(config))
    try:
        request = service.for_role('planner').request([], max_tokens=2000, thinking=True)
        assert request.max_tokens == 500 and request.reasoning is False
        request = service.for_role('thinker').request([], max_tokens=200, thinking=False)
        assert request.max_tokens == 200 and request.reasoning is False
    finally:
        await service.aclose()


async def test_compatible_provider_omits_ark_fields_and_preserves_raw_response_safely(monkeypatch):
    secret = 'custom-key-that-must-not-appear'
    monkeypatch.setenv('TEST_LLM_KEY', secret)
    profile = {'provider': 'openai_compatible', 'base_url': 'https://llm.test/v1', 'key_env': 'TEST_LLM_KEY'}
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert 'thinking' not in body
        return httpx.Response(200, json={'id': 'actual-request', 'model': 'test-model',
            'choices': [{'message': {'role': 'assistant', 'content': 'actual text'}, 'finish_reason': 'stop'}],
            'usage': {'completion_tokens': 2}, 'debug': {'Authorization': secret, 'echo': secret}})
    client = httpx.AsyncClient(base_url='https://llm.test/v1/', transport=httpx.MockTransport(respond))
    provider = ChatProvider(profile, transport=CompatibleTransport(profile, client))
    try:
        result = await provider.complete(LLMRequest([{'role': 'user', 'content': 'hello'}], model='test-model', reasoning=False, max_tokens=20))
        assert result.message['content'] == 'actual text'
        assert result.metadata['provider'] == 'openai_compatible'
        assert result.metadata['raw_response']['id'] == 'actual-request'
        assert result.metadata['actual_request'] == calls[0]
        assert secret not in json.dumps(result.metadata)
        assert len(calls) == 1
    finally:
        await provider.aclose()


async def test_compatible_stream_assembles_function_argument_deltas_once(monkeypatch):
    monkeypatch.setenv('TEST_LLM_KEY', 'local-secret')
    profile = {'provider': 'openai_compatible', 'base_url': 'https://llm.test/v1', 'key_env': 'TEST_LLM_KEY'}
    chunks = [
        {'id': 'upstream', 'choices': [{'delta': {'content': 'checking'}, 'finish_reason': None}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call-real', 'function': {'name': 'set_volume', 'arguments': '{"volume":'}}]}, 'finish_reason': None}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '30}'}}]}, 'finish_reason': 'tool_calls'}]},
        {'choices': [], 'usage': {'completion_tokens': 8}},
    ]
    wire = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n'
    client = httpx.AsyncClient(base_url='https://llm.test/v1/', transport=httpx.MockTransport(lambda r: httpx.Response(200, text=wire)))
    provider = ChatProvider(profile, transport=CompatibleTransport(profile, client))
    try:
        observed = [event async for event in provider.stream(LLMRequest([], model='test-model', reasoning=False, max_tokens=50))]
        final = observed[-1]
        assert final['message']['tool_calls'][0]['function'] == {'name': 'set_volume', 'arguments': '{"volume":30}'}
        assert final['message']['tool_calls'][0]['id'] == 'call-real'
        assert final['meta']['raw_chunks'] == chunks
        assert final['meta']['usage']['completion_tokens'] == 8
        assert final['meta']['actual_request']['model'] == 'test-model'
        assert 'thinking' not in final['meta']['actual_request']
        assert [event['text'] for event in observed if event['type'] == 'delta'] == ['checking']
    finally:
        await provider.aclose()


async def test_stream_error_preserves_redacted_partial_trace(monkeypatch):
    secret = 'x' * 280
    monkeypatch.setenv('TEST_LLM_KEY', secret)
    profile = {'provider': 'openai_compatible', 'base_url': 'https://llm.test/v1', 'key_env': 'TEST_LLM_KEY'}
    valid = {'choices': [{'delta': {'content': 'real partial'}, 'finish_reason': None}]}
    wire = 'data: ' + json.dumps(valid) + '\n\ndata: ' + secret + '\n\n'
    client = httpx.AsyncClient(base_url='https://llm.test/v1/', transport=httpx.MockTransport(lambda r: httpx.Response(200, text=wire)))
    provider = ChatProvider(profile, transport=CompatibleTransport(profile, client))
    try:
        with pytest.raises(ProviderError) as error:
            async for event in provider.stream(LLMRequest([], model='test-model', reasoning=False, max_tokens=50)):
                pass
        assert error.value.trace_partial['message']['content'] == 'real partial'
        serialized = json.dumps(error.value.trace_partial)
        assert secret[:240] not in serialized
        assert '[redacted]' in serialized
    finally:
        await provider.aclose()


@pytest.mark.parametrize('status,code', [(401, 'auth'), (403, 'auth'), (429, 'rate_limit'), (503, 'unavailable')])
async def test_search_transport_retains_structured_http_failure(monkeypatch, status, code):
    from backend.models import TavilyClient
    from backend.services.reads import TavilyProvider
    monkeypatch.setenv('CUSTOM_SEARCH_KEY', 'secret')
    async with httpx.AsyncClient(base_url='https://search.test/', transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={'error': {'code': 'vendor-error'}}))) as client:
        provider = TavilyProvider({'key_env': 'CUSTOM_SEARCH_KEY'}, TavilyClient(client, key_env='CUSTOM_SEARCH_KEY'))
        with pytest.raises(ProviderError) as failure:
            await provider.search('structured query')
        assert failure.value.code == code
        assert failure.value.details['status_code'] == status


async def test_missing_llm_credentials_is_not_configured_without_network(monkeypatch):
    monkeypatch.delenv('MISSING_LLM_KEY', raising=False)
    def handler(request):
        pytest.fail('Missing credentials must be detected before the network request')
    profile = {'provider': 'openai_compatible', 'base_url': 'https://llm.test/v1', 'key_env': 'MISSING_LLM_KEY'}
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = ChatProvider(profile, transport=CompatibleTransport(profile, client))
    try:
        with pytest.raises(ProviderError) as failure:
            await provider.complete(LLMRequest([], model='test'))
        assert failure.value.code == 'not_configured'
    finally:
        await provider.aclose()


async def test_configured_timeout_applies_to_nonstream_thinker_request(monkeypatch):
    monkeypatch.setenv('TEST_LLM_KEY', 'key')
    seen = []
    def handler(request):
        seen.append(request.extensions['timeout'])
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'actual'}}]})
    profile = {'provider': 'openai_compatible', 'base_url': 'https://llm.test/v1', 'key_env': 'TEST_LLM_KEY', 'timeout_seconds': 17}
    client = httpx.AsyncClient(base_url='https://llm.test/v1/', transport=httpx.MockTransport(handler))
    provider = ChatProvider(profile, transport=CompatibleTransport(profile, client))
    try:
        await provider.complete(LLMRequest([], role='thinker', model='test'))
        assert seen[0]['read'] == 17
    finally:
        await provider.aclose()


@pytest.mark.parametrize('body', [{'choices': []}, {'choices': [None]}, {'choices': [{'message': None}]}])
async def test_invalid_complete_shape_preserves_actual_body_as_typed_failure(monkeypatch, body):
    monkeypatch.setenv('TEST_LLM_KEY', 'key')
    profile = {'provider': 'openai_compatible', 'base_url': 'https://llm.test/v1', 'key_env': 'TEST_LLM_KEY'}
    client = httpx.AsyncClient(base_url='https://llm.test/v1/', transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    provider = ChatProvider(profile, transport=CompatibleTransport(profile, client))
    try:
        with pytest.raises(ProviderError) as failure:
            await provider.complete(LLMRequest([], model='test'))
        assert failure.value.code == 'invalid_response'
        assert failure.value.trace_partial['meta']['raw_response'] == body
    finally:
        await provider.aclose()


async def test_truncated_stream_is_not_reported_as_a_complete_response(monkeypatch):
    monkeypatch.setenv('TEST_LLM_KEY', 'key')
    chunk = {'choices': [{'delta': {'content': 'observed partial'}}]}
    profile = {'provider': 'openai_compatible', 'base_url': 'https://llm.test/v1', 'key_env': 'TEST_LLM_KEY'}
    client = httpx.AsyncClient(base_url='https://llm.test/v1/', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text='data: ' + json.dumps(chunk) + '\n\n')))
    provider = ChatProvider(profile, transport=CompatibleTransport(profile, client))
    try:
        with pytest.raises(ProviderError) as failure:
            _ = [part async for part in provider.stream(LLMRequest([], model='test'))]
        assert failure.value.code == 'invalid_response'
        assert failure.value.trace_partial['message']['content'] == 'observed partial'
        assert failure.value.trace_partial['meta']['raw_chunks'] == [chunk]
    finally:
        await provider.aclose()


async def test_structured_stream_error_is_not_an_empty_success(monkeypatch):
    monkeypatch.setenv('TEST_LLM_KEY', 'secret-value')
    chunk = {'error': {'code': 'server_error', 'message': 'actual provider diagnostic secret-value'}}
    profile = {'provider': 'openai_compatible', 'base_url': 'https://llm.test/v1', 'key_env': 'TEST_LLM_KEY'}
    client = httpx.AsyncClient(base_url='https://llm.test/v1/', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text='data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n')))
    provider = ChatProvider(profile, transport=CompatibleTransport(profile, client))
    try:
        with pytest.raises(ProviderError) as failure:
            _ = [part async for part in provider.stream(LLMRequest([], model='test'))]
        assert failure.value.code == 'upstream_error'
        assert failure.value.trace_partial['meta']['raw_chunks'][0]['error']['code'] == 'server_error'
        assert 'secret-value' not in json.dumps(failure.value.details)
    finally:
        await provider.aclose()


async def test_worker_step_limit_uses_configured_finalizer_role(tmp_path):
    from backend.db import Database
    from backend.worker import ThinkerWorker
    from tests.test_worker import FakeArk, FakeTavily, message, task
    class RoleAwareArk(FakeArk):
        def for_role(self, role):
            return self
    ark = RoleAwareArk(message('finish_task', {'summary': '没有查询新资料，使用已有信息收尾。'}))
    db = Database(tmp_path / 'worker-role.sqlite3')
    current = task(db)
    await ThinkerWorker(db, ark, FakeTavily(), max_steps=1).run_task(current)
    assert ark.options[0]['role'] == 'finalizer'
    assert ark.thinker_modes == [False]
    assert ark.options[0]['tool_choice'] == {'type': 'function', 'function': {'name': 'finish_task'}}
