"""Vertex uses the real stream/budget machinery with offline ADC and HTTP peers."""
import asyncio
from copy import deepcopy
import json
import threading
import tomllib

import google.auth
from google.auth.exceptions import DefaultCredentialsError, RefreshError
import httpx
import pytest

from backend.harness.assembly import FrozenNLPConfig, TaskBudget, model_policy_id
from backend.services.config import ServiceConfig, google_vertex_url
from backend.services.contracts import LLMRequest, ProviderError
from backend.services.google_auth import GoogleADCAuth
from backend.services.llm import ChatProvider, GoogleVertexTransport, LLMService
from backend.settings import ROOT


PROFILE = {'provider': 'google_vertex', 'project': 'offline-project', 'location': 'eu',
           'reasoning_effort': 'low', 'timeout_seconds': 45}
MODEL = 'google/gemini-3.8-flash'


def document():
    with (ROOT / 'config/services.toml').open('rb') as source:
        return tomllib.load(source)


class Credentials:
    def __init__(self, token='offline-access-token'):
        self.token, self.valid = token, False
        self.refreshes = 0

    def refresh(self, request):
        self.refreshes += 1
        self.valid = True


def install_adc(monkeypatch, credentials=None):
    credentials = credentials or Credentials()
    discovery = []
    def default(**options):
        discovery.append(options)
        return credentials, 'ignored-adc-default-project'
    monkeypatch.setattr(google.auth, 'default', default)
    return credentials, discovery


def response(message='offline answer'):
    return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': message}}]})


def provider(handler, *, auth=None, profile=None):
    profile = profile or PROFILE
    transport = GoogleVertexTransport(profile, auth=auth, transport=httpx.MockTransport(handler))
    return ChatProvider(profile, transport=transport)


@pytest.mark.parametrize('location,host', [
    ('eu', 'aiplatform.eu.rep.googleapis.com'), ('us', 'aiplatform.us.rep.googleapis.com'),
    ('global', 'aiplatform.googleapis.com'), ('europe-west9', 'europe-west9-aiplatform.googleapis.com'),
])
def test_vertex_endpoint_is_derived_from_explicit_project_and_location(location, host):
    assert google_vertex_url({**PROFILE, 'location': location}) == (
        f'https://{host}/v1/projects/offline-project/locations/{location}/endpoints/openapi')


@pytest.mark.parametrize('field,value', [
    ('project', '../other'), ('location', 'eu.evil.test'), ('location', ''),
    ('base_url', 'https://untrusted.test'), ('key_env', 'SOME_KEY'),
    ('reasoning_effort', 'off'), ('reasoning_effort', True),
])
def test_invalid_vertex_settings_are_rejected_before_requests(field, value):
    source = document()
    source['llm']['profiles']['google_vertex'] = {**PROFILE, field: value}
    with pytest.raises(ValueError):
        ServiceConfig(source)


@pytest.mark.parametrize('model,location,effort', [
    ('gemini-3.8-flash', 'eu', 'low'), (MODEL, 'europe-west9', 'low'),
    (MODEL, 'eu', 'minimal'), ('google/gemini-', 'eu', 'low'),
])
def test_selected_vertex_model_location_and_thinking_combination_is_validated(model, location, effort):
    source = document()
    source['llm']['profiles']['google_vertex'] = {**PROFILE, 'location': location, 'reasoning_effort': effort}
    source['llm']['roles']['talker'].update(profile='google_vertex', model=model)
    source['llm']['roles']['talker'].pop('model_env', None)
    with pytest.raises(ValueError):
        ServiceConfig(source)


def test_selected_vertex_requires_a_project_without_using_adc_project(monkeypatch):
    monkeypatch.delenv('GOOGLE_CLOUD_PROJECT', raising=False)
    source = document()
    source['llm']['roles']['talker'].update(profile='google_vertex', model=MODEL)
    source['llm']['roles']['talker'].pop('model_env', None)
    with pytest.raises(ValueError, match='project'):
        ServiceConfig(source)


async def test_inactive_vertex_profile_and_status_do_not_discover_adc(monkeypatch):
    monkeypatch.delenv('GOOGLE_CLOUD_PROJECT', raising=False)
    monkeypatch.setattr(google.auth, 'default', lambda **kwargs: pytest.fail('Inactive profile must not discover ADC'))
    service = LLMService(ServiceConfig(document()))
    try:
        assert service.info()['roles']['talker']['provider']['provider'] == 'ark'
        status = service.providers['google_vertex'].info()
        assert not status['configured'] and status['details']['credentials_probed'] is False
    finally:
        await service.aclose()


async def test_vertex_payload_auth_and_usage_preserve_actual_stream_evidence(monkeypatch):
    credentials, discovery = install_adc(monkeypatch)
    requests = []
    chunks = [
        {'choices': [{'delta': {'content': 'received'}, 'finish_reason': 'stop'}]},
        {'choices': [], 'usage': {'prompt_tokens': 100, 'completion_tokens': 4,
            'prompt_tokens_details': {'cached_tokens': 80}, 'completion_tokens_details': {'reasoning_tokens': 2}}},
    ]
    wire = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n'
    def handler(request):
        requests.append(request)
        assert request.headers['authorization'] == 'Bearer ' + credentials.token
        assert request.headers['x-goog-user-project'] == 'offline-project'
        return httpx.Response(200, text=wire)
    service = provider(handler)
    messages = [{'role': 'user', 'content': 'unchanged content'}]
    tools = [{'type': 'function', 'function': {'name': 'actual_tool', 'parameters': {'type': 'object'}}}]
    try:
        events = [item async for item in service.stream(LLMRequest(messages, tools, model=MODEL,
            reasoning=False, max_tokens=900, first_output_timeout_seconds=1, total_timeout_seconds=2))]
        payload = json.loads(requests[0].content)
        assert payload['messages'] == messages and payload['tools'] == tools
        assert payload['reasoning_effort'] == 'low' and payload['max_tokens'] == 900
        assert not {'thinking', 'temperature', 'top_p', 'top_k'} & payload.keys()
        assert payload['stream_options'] == {'include_usage': True}
        assert events[-1]['meta']['usage'] == chunks[-1]['usage']
        assert events[-1]['meta']['raw_chunks'] == chunks
        assert events[-1]['meta']['actual_request'] == payload
        assert events[-1]['meta']['provider'] == 'google_vertex'
        assert str(requests[0].url).startswith(google_vertex_url(PROFILE))
        assert 'Authorization' not in service.transport.client.headers
        assert len(discovery) == credentials.refreshes == 1
        assert discovery[0]['quota_project_id'] == 'offline-project'
    finally:
        await service.aclose()


async def test_model_default_reasoning_can_be_explicitly_left_unset(monkeypatch):
    install_adc(monkeypatch)
    profile = {key: value for key, value in PROFILE.items() if key != 'reasoning_effort'}
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return response()
    service = provider(handler, profile=profile)
    try:
        await service.complete(LLMRequest([], model=MODEL, reasoning=False))
        assert 'reasoning_effort' not in seen[0] and 'thinking' not in seen[0]
    finally:
        await service.aclose()


@pytest.mark.parametrize('failure_type,code', [(DefaultCredentialsError, 'not_configured'), (RefreshError, 'auth')])
async def test_adc_failure_is_typed_redacted_and_never_sends_model_request(monkeypatch, failure_type, code):
    def default(**options):
        raise failure_type('sensitive-credential-sdk-diagnostic')
    monkeypatch.setattr(google.auth, 'default', default)
    service = provider(lambda request: pytest.fail('No model request without ADC'))
    try:
        with pytest.raises(ProviderError) as failure:
            _ = [item async for item in service.stream(LLMRequest([], model=MODEL))]
        assert failure.value.code == code
        assert failure.value.details['error_type'] == failure_type.__name__
        assert 'sensitive-credential' not in str(failure.value) + json.dumps(failure.value.trace_partial)
    finally:
        await service.aclose()


@pytest.mark.parametrize('stream', [True, False])
async def test_adc_refresh_is_inside_model_deadline_and_cannot_later_dispatch(monkeypatch, stream):
    entered, release = threading.Event(), threading.Event()
    credentials = Credentials()
    def refresh(request):
        entered.set()
        assert release.wait(2)
        credentials.valid = True
    credentials.refresh = refresh
    install_adc(monkeypatch, credentials)
    service = provider(lambda request: pytest.fail('Expired model deadline must not dispatch'))
    request = LLMRequest([], model=MODEL, first_output_timeout_seconds=.02, total_timeout_seconds=.03)
    try:
        with pytest.raises(ProviderError) as failure:
            if stream:
                _ = [item async for item in service.stream(request)]
            else:
                await service.complete(request)
        assert entered.is_set() and failure.value.code == 'timeout'
        expected_phase = 'first_output' if stream else 'total'
        assert failure.value.trace_partial['meta']['deadline_phase'] == expected_phase
        assert failure.value.trace_partial['meta']['elapsed_ms'] < 1000
    finally:
        release.set()
        task = service.transport.auth._refresh_task
        if task is not None:
            await task
        await service.aclose()


async def test_cancelled_refresh_waiter_does_not_cancel_shared_refresh_or_send_request(monkeypatch):
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    credentials, discovery = install_adc(monkeypatch)
    def refresh(request):
        credentials.refreshes += 1
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        credentials.valid = True
    credentials.refresh = refresh
    sent = []
    def handler(request):
        sent.append(request)
        return response()
    service = provider(handler)
    try:
        first = asyncio.create_task(service.complete(LLMRequest([], model=MODEL)))
        await entered.wait()
        second = asyncio.create_task(service.complete(LLMRequest([], model=MODEL)))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert (await second).message['content'] == 'offline answer'
        assert len(sent) == len(discovery) == credentials.refreshes == 1
    finally:
        release.set()
        await service.aclose()


async def test_token_rotation_is_request_scoped_with_concurrent_inflight_calls(monkeypatch):
    credentials, _ = install_adc(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []
    async def handler(request):
        seen.append(request.headers['authorization'])
        if len(seen) == 1:
            entered.set()
            await release.wait()
        return response()
    service = provider(handler)
    try:
        first = asyncio.create_task(service.complete(LLMRequest([], model=MODEL)))
        await entered.wait()
        credentials.valid = False
        credentials.token = 'second-offline-token'
        await service.complete(LLMRequest([], model=MODEL))
        release.set()
        await first
        assert seen == ['Bearer offline-access-token', 'Bearer second-offline-token']
        assert credentials.refreshes == 2
        assert set(service.secrets) >= {'offline-access-token', 'second-offline-token'}
        assert 'authorization' not in service.transport.client.headers
    finally:
        release.set()
        await service.aclose()


async def test_long_access_token_is_redacted_before_error_diagnostic_truncation(monkeypatch):
    secret = 'offline-' + 's' * 300
    install_adc(monkeypatch, Credentials(secret))
    sent = []
    def handler(request):
        sent.append(request)
        return httpx.Response(403, json={'error': {'code': 403, 'message': secret + ' denied'}})
    service = provider(handler)
    try:
        with pytest.raises(ProviderError) as failure:
            _ = [item async for item in service.stream(LLMRequest([], model=MODEL))]
        assert failure.value.code == 'permission_denied' and len(sent) == 1
        serialized = json.dumps({'details': failure.value.details, 'partial': failure.value.trace_partial})
        assert secret[:200] not in serialized and '[redacted]' in serialized
    finally:
        await service.aclose()


async def test_non_http_diagnostic_redacts_token_before_short_attempt_summary(monkeypatch):
    secret = 'offline-' + 's' * 300
    install_adc(monkeypatch, Credentials(secret))
    def handler(request):
        raise ValueError(secret + ' malformed provider frame')
    service = provider(handler)
    try:
        with pytest.raises(ProviderError) as failure:
            _ = [item async for item in service.stream(LLMRequest([], model=MODEL))]
        assert failure.value.code == 'invalid_response'
        serialized = json.dumps({'details': failure.value.details, 'partial': failure.value.trace_partial})
        assert secret[:200] not in serialized and '[redacted]' in serialized
    finally:
        await service.aclose()


def test_google_tts_config_is_explicit_and_cannot_redirect_adc(monkeypatch):
    source = document()
    source['tts']['profile'] = 'google'
    monkeypatch.delenv('GOOGLE_CLOUD_PROJECT', raising=False)
    monkeypatch.delenv('TTS_PROFILE', raising=False)
    with pytest.raises(ValueError, match='project'):
        ServiceConfig(source)
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'offline-project')
    assert ServiceConfig(source).profile('tts')['location'] == 'eu'
    source['tts']['profiles']['google']['base_url'] = 'https://untrusted.test'
    with pytest.raises(ValueError, match='fixed location'):
        ServiceConfig(source)


@pytest.mark.parametrize('field,value', [('project', 'changed-project'), ('location', 'us'), ('reasoning_effort', 'medium')])
async def test_vertex_deployment_identity_is_frozen_and_changes_model_policy(monkeypatch, field, value):
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'offline-project')
    source = document()
    source['llm']['roles']['talker'].update(profile='google_vertex', model=MODEL)
    source['llm']['roles']['talker'].pop('model_env', None)
    frozen = FrozenNLPConfig(ServiceConfig(source))
    first = LLMService(frozen)
    budget = TaskBudget(7, 31, 9, 4)
    try:
        before = model_policy_id(first, frozen, budget)
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'later-environment-project')
        assert frozen.document['llm']['profiles']['google_vertex']['project'] == 'offline-project'
        assert model_policy_id(first, frozen, budget) == before
        monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'offline-project')
        changed = deepcopy(source)
        if field == 'project':
            changed['llm']['profiles']['google_vertex'].pop('project_env', None)
        changed['llm']['profiles']['google_vertex'][field] = value
        replacement = FrozenNLPConfig(ServiceConfig(changed))
        second = LLMService(replacement)
        try:
            assert model_policy_id(second, replacement, budget) != before
        finally:
            await second.aclose()
    finally:
        await first.aclose()


async def test_complete_cancellation_redacts_observed_partial_diagnostic():
    secret = 'offline-token-' + 'z' * 300
    class CancelTransport:
        secrets = (secret,)
        async def complete(self, *args, **kwargs):
            error = asyncio.CancelledError()
            error.trace_partial = {'message': {'content': secret}, 'meta': {'raw_response': {'echo': secret}}}
            raise error
    service = ChatProvider(PROFILE, transport=CancelTransport())
    with pytest.raises(asyncio.CancelledError) as failure:
        await service.complete(LLMRequest([], model=MODEL))
    serialized = json.dumps(failure.value.trace_partial)
    assert secret[:200] not in serialized and '[redacted]' in serialized
