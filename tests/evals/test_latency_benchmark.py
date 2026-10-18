"""Offline latency gates keep failures and exercise the real NLPService path."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from super_xd.nlp.contracts.providers import ProviderError
from evals import gateway_driver, latency
from tests.support.gateway_contract import call


class OfflineReads:
    def info(self):
        return {'configured': True, 'execution_location': 'cloud'}

    async def aclose(self):
        pass


@pytest.fixture(autouse=True)
def no_external_reads(monkeypatch):
    for name in ('create_maps', 'create_search', 'ManualSearchClient'):
        monkeypatch.setattr(gateway_driver, name, OfflineReads)


class ChatModel:
    def __init__(self, responses=None, clock=None):
        self.responses = responses
        self.requests, self.active, self.closed = [], 0, False
        self.clock = clock
        self.config = SimpleNamespace(source='offline')

    def info(self):
        return {'roles': {'talker': {'model': 'offline-fixed-model', 'provider': {'configured': True}}}}

    async def stream(self, request):
        messages = request.messages
        tools = request.tools
        options = {key: value for key, value in vars(request).items() if key not in {'messages', 'tools'} and value is not None}
        assert self.active == 0, 'The benchmark must not issue parallel requests.'
        self.active += 1
        self.requests.append(deepcopy({'messages': messages, 'tools': tools, 'options': options}))
        response = self.responses[len(self.requests) - 1] if self.responses is not None else {'content': '很高兴和你聊聊。', 'cached': 0}
        try:
            if self.clock is not None:
                self.clock[0] += .05
            if response.get('content'):
                yield {'type': 'delta', 'text': response['content']}
            if response.get('error'):
                raise response['error']
            usage = {'prompt_tokens': 200}
            if 'cached' in response:
                usage['prompt_tokens_details'] = {'cached_tokens': response['cached']}
            message = {'role': 'assistant', 'content': response.get('content')}
            if response.get('tool_calls'):
                message['tool_calls'] = response['tool_calls']
            yield {'type': 'message', 'message': message, 'meta': {'usage': usage}}
        finally:
            self.active -= 1

    async def aclose(self):
        self.closed = True


async def test_serial_pause_uses_real_wait_and_records_actual_idle_with_full_context(monkeypatch):
    clock, waits = [100.0], []
    model = ChatModel(clock=clock)

    async def sleep(seconds):
        assert model.active == 0
        waits.append(seconds)
        clock[0] += seconds + .125  # Actual scheduler delay is reported, not the requested value.

    monkeypatch.setattr(latency, 'time', SimpleNamespace(perf_counter=lambda: clock[0]))
    monkeypatch.setattr(latency, 'asyncio', SimpleNamespace(sleep=sleep))
    report = await latency.run_benchmark(model, repeat=2, pause_seconds=3)
    assert waits == [3] * 5
    assert len(model.requests) == len(report['samples']) == 6
    assert all(len(request['tools']) == 26 for request in model.requests)
    assert [row['input'] for row in report['samples']] == [text for _, text in latency.INPUTS] * 2
    assert len({row['input'] for row in report['samples']}) >= 3
    assert report['samples'][0]['actual_idle_ms'] is None
    assert [row['actual_idle_ms'] for row in report['samples'][1:]] == pytest.approx([3125] * 5)
    assert all(row['release_id'] == report['release']['release_id'] for row in report['samples'])
    assert len({row['model_policy_id'] for row in report['samples']}) == 1
    assert all(row['passed'] and row['first_model_text_ms'] is not None for row in report['samples'])
    assert report['summary']['cadence_groups']['first_request']['turns'] == 1
    assert report['summary']['cadence_groups']['after_pause']['turns'] == 5
    assert report['summary']['cadence_groups']['consecutive']['turns'] == 0
    # Fresh conversations retain examples but never previous benchmark answers.
    assert all(not any(message.get('content') == '很高兴和你聊聊。' for message in request['messages'])
               for request in model.requests)
    assert not model.closed, 'The caller owns the shared provider pool.'


async def test_failed_fallback_partial_failure_and_missing_usage_are_distinct_and_retained():
    model = ChatModel([
        {'error': ProviderError('timeout', 'offline timeout', provider='fixture')},
        {'content': '我收到了', 'error': ProviderError('upstream_error', 'offline reset', provider='fixture')},
        {'content': '你好呀。'},
    ])
    report = await latency.run_benchmark(model, repeat=1)
    timeout, partial, success = report['samples']
    assert timeout['first_model_text_ms'] is None and timeout['first_effective_ms'] is not None
    assert timeout['presentation_source'] == partial['presentation_source'] == 'error_template'
    assert partial['first_model_text_ms'] is not None and not partial['passed']
    assert success['passed'] and success['presentation_source'] is None
    assert all(row['cache_group'] == 'unknown' and row['first_talker_cached_tokens'] is None for row in report['samples'])
    summary = report['summary']
    assert summary['turns'] == summary['model_calls'] == 3
    assert summary['failed'] == summary['error_turns'] == summary['model_text_samples'] == 2
    assert summary['no_model_text_turns'] == summary['successful_first_model_text_samples'] == 1
    assert summary['cadence_groups']['consecutive']['turns'] == 2
    assert timeout['trace'] and partial['trace'] and timeout['model_calls'] == partial['model_calls'] == 1


async def test_unexpected_hot_tool_cannot_reach_even_the_simulator(monkeypatch):
    async def forbidden_execute(*args, **kwargs):
        pytest.fail('No device, lookup or media executor may run in a chat latency benchmark.')

    monkeypatch.setattr(gateway_driver.EvaluationGateway, 'execute', forbidden_execute)
    model = ChatModel([{'tool_calls': [call('set_volume', {'volume': 40})], 'cached': 0},
                       {'content': '收到。', 'cached': 50}, {'content': '好的。'}])
    report = await latency.run_benchmark(model, repeat=1)
    mistaken = report['samples'][0]
    assert mistaken['error'] == 'unexpected_tool_request'
    assert 'unexpected_tool_request' in mistaken['failures'] and not mistaken['passed']
    assert not mistaken['commands'] and not mistaken['lookups']
    assert mistaken['vehicle'] == mistaken['vehicle_before']
    assert mistaken['model_calls'] == 1 and mistaken['trace']
    groups = report['summary']['cache_groups']
    assert groups['zero_cache']['turns'] == groups['cache_hit']['turns'] == groups['unknown']['turns'] == 1


def sample(first, *, error=None, cache='zero_cache'):
    observed = {'first_model_text_ms': first, 'first_effective_ms': first if first is not None else 10000,
                'total_ms': 10000 if error else first + 20, 'error': error, 'model_calls': 1,
                'cadence_group': 'after_pause', 'cache_group': cache}
    failures = latency.failures_for(observed, 2000)
    return {**observed, 'passed': not failures, 'failures': failures,
            'response_succeeded': not any(reason != 'first_model_text_threshold_exceeded' for reason in failures)}


def test_slow_and_failed_samples_remain_in_denominators_and_success_percentiles():
    summary = latency.benchmark_summary([sample(1000), sample(4000),
        sample(None, error='timeout', cache='unknown'), sample(300, error='reset', cache='cache_hit')])
    assert summary['turns'] == 4 and summary['passed'] == 1 and summary['failed'] == 3
    assert summary['model_text_samples'] == 3 and summary['no_model_text_turns'] == 1
    assert summary['successful_response_turns'] == summary['successful_first_model_text_samples'] == 2
    assert summary['successful_first_model_text_p90'] == 4000
    assert summary['first_model_text_p90'] == 4000
    assert summary['cache_groups']['unknown']['total_p90'] == 10000
    assert summary['cadence_cache_groups']['after_pause']['zero_cache']['failed'] == 1


def test_later_completion_cannot_supply_missing_first_call_cache_usage():
    rows = [{'kind': 'model.completed', 'turn_id': 'turn', 'data': {'meta': {'usage': usage}}}
            for usage in ({}, {'prompt_tokens_details': {'cached_tokens': 99}})]
    assert latency.first_talker_cached_tokens({'turn_id': 'turn', 'trace': rows}) is None
    rows[0]['data']['meta']['usage'] = {'cached_tokens': 0}
    assert latency.first_talker_cached_tokens({'turn_id': 'turn', 'trace': rows}) == 0


@pytest.mark.parametrize('bad', [['--repeat', '0'], ['--pause-seconds', '-1'], ['--pause-seconds', 'nan'],
                               ['--max-first-text-ms', 'inf'], ['--max-first-text-ms', '0']])
async def test_invalid_cli_options_do_not_construct_a_provider(monkeypatch, tmp_path, bad):
    import super_xd.nlp.adapters.factory as factory
    monkeypatch.setattr(factory, 'create_llm', lambda: pytest.fail('Provider construction before input validation'))
    with pytest.raises(SystemExit) as error:
        await latency.main(['--out', str(tmp_path / 'report.json'), *bad])
    assert error.value.code == 2


@pytest.mark.parametrize('threshold,expected', [(None, 0), ('0.000001', 1)])
async def test_cli_writes_complete_report_returns_latency_gate_and_closes_pool(monkeypatch, tmp_path, threshold, expected):
    import super_xd.nlp.adapters.factory as factory
    model = ChatModel()
    monkeypatch.setattr(factory, 'create_llm', lambda: model)
    output = tmp_path / 'nested' / 'latency.json'
    arguments = ['--repeat', '1', '--out', str(output)]
    if threshold:
        arguments += ['--max-first-text-ms', threshold]
    assert await latency.main(arguments) == expected
    report = json.loads(output.read_text())
    assert len(report['samples']) == report['summary']['turns'] == 3
    assert report['summary']['failed'] == (3 if expected else 0)
    assert report['model_configuration']['roles']['talker']['model'] == 'offline-fixed-model'
    assert model.closed


@pytest.mark.parametrize('response', [{}, {'error': ProviderError('timeout', 'offline timeout', provider='fixture')}])
async def test_cli_fails_without_model_text_even_when_no_latency_threshold_is_set(monkeypatch, tmp_path, response):
    import super_xd.nlp.adapters.factory as factory
    model = ChatModel([response] * 3)
    monkeypatch.setattr(factory, 'create_llm', lambda: model)
    output = tmp_path / 'missing-text.json'
    assert await latency.main(['--repeat', '1', '--out', str(output)]) == 1
    report = json.loads(output.read_text())
    assert report['summary']['failed'] == report['summary']['no_model_text_turns'] == 3
    assert report['summary']['successful_first_model_text_samples'] == 0
    assert report['summary']['successful_first_model_text_p50'] is None
    assert model.closed
