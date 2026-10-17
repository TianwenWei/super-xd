"""Evaluation entrypoints use configured credentials and exact experiment models."""
from copy import deepcopy
import json
from types import SimpleNamespace
import tomllib

import httpx
import pytest

from backend.services import config as service_config, factory
from backend.settings import ROOT
from evals import speech_review
from scripts import validate_structured_prompts as script


@pytest.fixture
def configured_provider(monkeypatch):
    document = tomllib.loads((ROOT / 'config/services.toml').read_text())
    document['llm']['profiles'] = {'selected': {'provider': 'ark', 'base_url': 'https://selected.test/v3',
                                              'key_env': 'EVAL_SELECTED_KEY', 'timeout_seconds': 17}}
    for role, setting in document['llm']['roles'].items():
        setting.update(profile='selected', model='configured-' + role, max_tokens=77)
        setting.pop('model_env', None)
    config = service_config.ServiceConfig(document)
    monkeypatch.setattr(factory, 'load_services_config', lambda: config)
    monkeypatch.setattr(service_config, 'load_services_config', lambda: config)
    monkeypatch.setenv('EVAL_SELECTED_KEY', 'selected-test-only-key')
    monkeypatch.setenv('ARK_API_KEY', 'obsolete-test-only-key')
    requests, clients = [], []

    def respond(request):
        assert request.url.host == 'selected.test'
        assert request.headers['Authorization'] == 'Bearer selected-test-only-key'
        body = json.loads(request.content)
        requests.append(body)
        text = (json.dumps({'preface_claims_completion': False, 'natural': 4, 'issue': ''})
                if body['messages'][0]['content'] == speech_review.PROMPT else 'offline answer')
        if body['stream']:
            chunks = [{'model': body['model'], 'choices': [{'delta': {'content': text}, 'finish_reason': None}]},
                      {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}]
            wire = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n'
            return httpx.Response(200, text=wire)
        return httpx.Response(200, json={'model': body['model'], 'choices': [
            {'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}]})

    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(respond)
        result = original_client(*args, **kwargs)
        clients.append(result)
        return result

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    return SimpleNamespace(config=config, requests=requests, clients=clients)


@pytest.mark.parametrize('override,expected', [(None, 'configured-thinker'), ('experiment-judge', 'experiment-judge')])
async def test_speech_review_uses_selected_profile_and_honors_explicit_model(configured_provider, tmp_path, override, expected):
    source, destination = tmp_path / 'source.json', tmp_path / 'speech.json'
    source.write_text(json.dumps({'results': [{'turns': [{'input': '用户请求', 'preface': '这就处理', 'text': '这就处理'}]}]}))
    arguments = [str(source), '--out', str(destination)] + (['--model', override] if override else [])
    await speech_review.main(arguments)
    assert configured_provider.requests[0]['model'] == expected
    assert configured_provider.requests[0]['thinking']['type'] == 'disabled'
    report = json.loads(destination.read_text())
    assert report['model_configuration']['source'] == configured_provider.config.source
    assert report['reviews'][0]['model_execution'] == {'profile': 'selected', 'provider': 'ark', 'model': expected}
    assert report['reviews'][0]['response_metadata']['actual_request']['model'] == expected
    assert all(client.is_closed for client in configured_provider.clients)


def captured_case(*, stream=True, thinker=False):
    request = {'messages': [{'role': 'user', 'content': 'offline request'}], 'tools': [],
               'model': 'historical-experiment-model', 'stream': stream, 'thinker': thinker,
               'max_tokens': 913, 'temperature': 0,
               'thinking': {'type': 'enabled' if thinker else 'disabled'}}
    return {'id': 'historical-case', 'role': 'thinker_research' if thinker else 'talker', 'text': 'offline request',
            'expected': {'calls': []}, 'request': request, 'request_sha256': script.digest(request)}


@pytest.mark.parametrize('stream,thinker', [(True, False), (False, True)])
async def test_replay_preserves_captured_model_and_options_for_both_transports(configured_provider, tmp_path, stream, thinker):
    source, destination = tmp_path / 'capture.json', tmp_path / 'replay.json'
    case = captured_case(stream=stream, thinker=thinker)
    source.write_text(json.dumps({'scope': 'offline transport test', 'cases': [case]}))
    await script.replay(SimpleNamespace(input=str(source), output=str(destination), concurrency=1, only=None))
    actual = configured_provider.requests[0]
    assert actual['model'] == case['request']['model']
    assert actual['thinking'] == case['request']['thinking']
    assert actual['max_tokens'] == 913  # Replay is not silently clamped to current role's 77.
    assert actual['stream'] is stream
    report = json.loads(destination.read_text())
    assert report['model_configuration']['source'] == configured_provider.config.source
    attempt = report['attempts'][0]
    assert attempt['automatic_pass'] and attempt['request'] == case['request']
    assert attempt['model_execution']['profile'] == 'selected'
    assert attempt['response']['meta']['actual_request']['model'] == case['request']['model']
    assert all(client.is_closed for client in configured_provider.clients)


@pytest.mark.parametrize('changes', [{'temperature': 0.7}, {'model': ''}, {'max_tokens': 0},
                                     {'thinking': {'type': 'automatic'}}, {'unknown_option': True}])
async def test_unrepresentable_capture_options_fail_without_provider_calls(configured_provider, tmp_path, changes):
    case = captured_case()
    case['request'].update(changes)
    source, destination = tmp_path / 'capture.json', tmp_path / 'replay.json'
    source.write_text(json.dumps({'scope': 'offline transport test', 'cases': [case]}))
    await script.replay(SimpleNamespace(input=str(source), output=str(destination), concurrency=1, only=None))
    assert not configured_provider.requests
    attempt = json.loads(destination.read_text())['attempts'][0]
    assert not attempt['automatic_pass'] and attempt['error']


async def test_offline_capture_reads_configured_model_names_without_constructing_clients(configured_provider, tmp_path, monkeypatch):
    runtime = script.import_runtime()
    monkeypatch.setattr(runtime.session, 'utcnow', runtime.session.utcnow)
    source, destination = tmp_path / 'suite.json', tmp_path / 'capture.json'
    source.write_text(json.dumps({'version': 'test', 'scope': 'offline test',
        'fixed_current_utc': '2026-10-14T12:00:00+00:00', 'cases': [
            {'id': 'talk', 'role': 'talker', 'text': '你好'},
            {'id': 'quick', 'role': 'thinker_quick', 'text': '核实天气'},
            {'id': 'research', 'role': 'thinker_research', 'text': '调研信息'}]}))
    await script.capture(SimpleNamespace(cases=str(source), output=str(destination)))
    report = json.loads(destination.read_text())
    assert [case['request']['model'] for case in report['cases']] == ['configured-talker', 'configured-talker', 'configured-thinker']
    assert not configured_provider.clients and not configured_provider.requests


async def test_replay_rejects_reasoning_if_selected_adapter_cannot_encode_it(configured_provider):
    config = deepcopy(configured_provider.config.document)
    config['llm']['profiles']['selected']['provider'] = 'openai_compatible'
    llm = factory.create_llm(service_config.ServiceConfig(config))
    try:
        with pytest.raises(ValueError, match='cannot represent'):
            script.replay_request(llm, captured_case(stream=False, thinker=True)['request'], 'thinker_research')
        assert not configured_provider.requests
    finally:
        await llm.aclose()


@pytest.mark.parametrize('case_role,selected_role', [('vehicle_planner', 'planner'), ('planner', 'planner'),
                                                   ('finalizer', 'finalizer'), ('thinker_quick', 'talker')])
async def test_case_role_selects_profile_independently_of_reasoning_flag(configured_provider, case_role, selected_role):
    config = deepcopy(configured_provider.config.document)
    original = config['llm']['profiles']['selected']
    config['llm']['profiles'] = {role: deepcopy(original) for role in config['llm']['roles']}
    for role, setting in config['llm']['roles'].items():
        setting['profile'] = role
    llm = factory.create_llm(service_config.ServiceConfig(config))
    try:
        provider, exact, route = script.replay_request(llm, captured_case()['request'], case_role)
        assert provider is llm.providers[selected_role]
        assert route['profile'] == selected_role and exact.role == selected_role
        assert exact.model == 'historical-experiment-model'
        assert exact.reasoning is False
    finally:
        await llm.aclose()


@pytest.mark.parametrize('concurrency', [0, -1])
async def test_invalid_concurrency_is_rejected_before_loading_data_or_clients(configured_provider, concurrency):
    with pytest.raises(SystemExit) as error:
        await speech_review.main(['missing-file.json', '--concurrency', str(concurrency)])
    assert error.value.code == 2
    with pytest.raises(ValueError, match='concurrency'):
        await script.replay(SimpleNamespace(concurrency=concurrency))
    assert not configured_provider.clients
