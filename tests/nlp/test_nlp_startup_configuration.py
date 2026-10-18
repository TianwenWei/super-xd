"""Production configuration, liveness/readiness and aggregate model observations."""
from super_xd.nlp.contracts.providers import ModelRequest
from tests.support.models import scripted_result
from contextlib import aclosing
import os
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from super_xd.nlp.observability import MeasuredLLM, Readiness, UsageMetrics
from super_xd.nlp.api.app import create_app
from super_xd.nlp.settings import NLPSettings


def test_settings_are_explicit_frozen_and_secrets_are_not_printed():
    settings = NLPSettings.from_environment({'NLP_GATEWAY_URL': 'https://gateway.test/',
        'NLP_GATEWAY_TOKEN': 'offline-secret', 'NLP_MAX_INFLIGHT': '5', 'THINKER_MAX_STEPS': '8'})
    assert settings.gateway_url == 'https://gateway.test' and settings.max_inflight == 5
    assert settings.thinker_max_steps == 8
    assert 'offline-secret' not in repr(settings) and 'offline-secret' not in settings.model_dump_json()
    with pytest.raises(ValueError):
        settings.max_inflight = 3


@pytest.mark.parametrize('values', [
    {'NLP_MAX_INFLIGHT': '0'}, {'NLP_MAX_BODY_BYTES': '-1'}, {'NLP_BODY_TIMEOUT_SECONDS': 'nan'},
    {'NLP_GATEWAY_TIMEOUT_SECONDS': 'inf'}, {'THINKER_TIMEOUT_SECONDS': '0'},
    {'NLP_GATEWAY_URL': 'https://user:secret@gateway.test'},
    {'NLP_GATEWAY_HEALTH_URL': 'https://gateway.test/health?secret=fixture'},
])
def test_invalid_startup_settings_fail_before_opening_clients(values):
    with pytest.raises(ValueError):
        NLPSettings.from_environment(values)


def test_production_settings_import_does_not_load_dotenv():
    code = '''
import sys
class NoDotenv:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'dotenv' or fullname.startswith('dotenv.'):
            raise AssertionError('implicit dotenv import')
sys.meta_path.insert(0, NoDotenv())
import super_xd.nlp.defaults
import super_xd.nlp.settings
assert 'dotenv' not in sys.modules
'''
    subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True)


def test_local_bootstrap_is_explicit_and_does_not_override_exported_values(tmp_path, monkeypatch):
    from super_xd.demo.environment import load_local_environment
    monkeypatch.delenv('PYTHON_DOTENV_DISABLED', raising=False)
    path = tmp_path / '.env'
    path.write_text('BOOTSTRAP_FIXTURE=local\n')
    monkeypatch.setenv('BOOTSTRAP_FIXTURE', 'exported')
    load_local_environment(path)
    assert os.environ['BOOTSTRAP_FIXTURE'] == 'exported'
    load_local_environment(path, override=True)
    assert os.environ['BOOTSTRAP_FIXTURE'] == 'local'


async def test_optional_readiness_probe_is_cached_and_cannot_send_credentials_off_origin():
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(503)
    settings = NLPSettings(gateway_url='https://gateway.test', gateway_health_url='https://health.test/ready',
                           gateway_token='offline-secret')
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        ready = Readiness(settings, client)
        assert (await ready.check(local_ready=False))['gateway'] == 'not_probed'
        first, second = await ready.check(local_ready=True), await ready.check(local_ready=True)
    assert first == second == {'ready': False, 'local': 'ready', 'gateway': 'failed'}
    assert len(seen) == 1 and 'authorization' not in seen[0].headers


async def test_explicit_gateway_probe_success_authentication_and_recovery():
    statuses = iter([503, 200])
    def respond(request):
        assert request.headers['Authorization'] == 'Bearer offline-secret'
        return httpx.Response(next(statuses))
    settings = NLPSettings(gateway_url='https://gateway.test', gateway_health_url='https://gateway.test/ready',
                           gateway_token='offline-secret', readiness_cache_seconds=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        ready = Readiness(settings, client)
        assert (await ready.check(local_ready=True))['ready'] is False
        assert (await ready.check(local_ready=True))['ready'] is True


async def test_live_is_independent_of_startup_and_ready_reports_actual_local_scope():
    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://nlp.test') as client:
        assert (await client.get('/health/live')).json() == {'status': 'alive'}
        assert (await client.get('/health/ready')).status_code == 503
        assert (await client.get('/metrics')).json() == {'scope': 'process', 'models': {}, 'inflight': 0, 'max_inflight': 32}


class FakeModel:
    config, providers = object(), {}
    async def complete(self, request):
        return scripted_result({'role': 'assistant', 'content': 'private response',
                '_meta': {'usage': {'prompt_tokens': 7, 'completion_tokens': 3}, 'provider_id': 'private-id'}})
    async def stream(self, request):
        yield {'type': 'delta', 'text': 'private response'}
        yield {'type': 'message', 'message': {'role': 'assistant', 'content': 'private response'},
               'meta': {'usage': {'input_tokens': 5, 'output_tokens': 2}}}


async def test_model_metrics_count_actual_calls_tokens_and_cancellation_without_content():
    metrics = UsageMetrics()
    model = MeasuredLLM(FakeModel(), metrics)
    await model.complete(ModelRequest([{'role': 'user', 'content': 'private question'}], role='planner'))
    items = [item async for item in model.stream(ModelRequest([]))]
    assert items[-1]['message']['content'] == 'private response'
    async with aclosing(model.stream(ModelRequest([]))) as stream:
        await anext(stream)
    snapshot = metrics.snapshot()
    assert snapshot['models']['planner']['calls'] == 1
    assert snapshot['models']['planner']['input_tokens'] == 7
    assert snapshot['models']['talker']['calls'] == 2
    assert snapshot['models']['talker']['completed'] == 1 and snapshot['models']['talker']['cancelled'] == 1
    assert snapshot['models']['talker']['usage_reports'] == 1
    assert 'private' not in str(snapshot)


async def test_model_failures_are_counted_without_inventing_token_usage():
    class FailedModel(FakeModel):
        async def complete(self, *args, **kwargs):
            raise RuntimeError('private provider diagnostic')
    metrics = UsageMetrics()
    with pytest.raises(RuntimeError):
        await MeasuredLLM(FailedModel(), metrics).complete(ModelRequest([]))
    counters = metrics.snapshot()['models']['talker']
    assert counters['calls'] == counters['failed'] == 1
    assert counters['usage_reports'] == 0


@pytest.mark.parametrize('code', ['start_size_exceeded', 'checkpoint_size_exceeded', 'observations_size_exceeded', 'other'])
async def test_validation_reports_only_whitelisted_size_codes_without_inputs(code):
    import json
    from fastapi.exceptions import RequestValidationError
    app = create_app()
    error = RequestValidationError([{'type': code, 'input': 'private request', 'loc': ['body']}])
    response = await app.exception_handlers[RequestValidationError](None, error)
    assert json.loads(response.body) == {'error_code': code if code != 'other' else 'invalid_request', 'error_count': 1}
    assert b'private' not in response.body
