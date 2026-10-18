"""Tool-roundtrip gates use verified results and preserve every paced sample."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from super_xd.nlp.contracts.providers import ProviderError
from evals import gateway_driver, harness
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


@pytest.mark.parametrize('useful', [None, False, -1, float('nan'), float('inf'), '10'])
def test_missing_or_invalid_useful_result_cannot_be_replaced_by_fast_preface(useful):
    observed = {'first_useful_ms': useful, 'first_effective_ms': 1, 'first_model_text_ms': 1,
                'preface': '正在处理。'}
    assert harness.useful_latency_failures(observed, 2000) == [
        'first-useful gate: first_useful_ms missing or invalid']
    assert harness.useful_latency_failures(observed, None) == []


@pytest.mark.parametrize('failure', [{'error': 'timeout'}, {'model_errors': [{'kind': 'model.error'}]},
                                    {'outcome_unknown': True}, {'presentation_source': 'error_template'}])
def test_error_cannot_pass_even_with_a_verified_partial_result(failure):
    assert harness.useful_latency_failures({'first_useful_ms': 100, **failure}, 2000) == [
        'first-useful gate: turn has an error or unknown outcome']


def test_threshold_includes_boundary_and_never_overwrites_behavior_failures():
    assert harness.useful_latency_failures({'first_useful_ms': 2000}, 2000) == []
    assert harness.useful_latency_failures({'first_useful_ms': 2000.1}, 2000) == [
        'first-useful gate: 2000.1 ms exceeds 2000 ms']


async def test_serial_pause_spans_turns_cases_and_repeats_but_not_tool_continuations(monkeypatch):
    clock, waits, requests = [100.0], [], []
    active = False

    class Model:
        async def stream(self, request):
            messages = request.messages
            tools = request.tools
            nonlocal active
            assert not active
            active = True
            requests.append({'at': clock[0], 'messages': deepcopy(messages), 'tools': deepcopy(tools)})
            query = len(requests) % 2 == 1
            text = '正在查询公开资料。' if query else '这份资料列出了相关信息。'
            message = {'role': 'assistant', 'content': text}
            if query:
                message['tool_calls'] = [call('web_search', {'query': '公开资料'}, identity=f'q-{len(requests)}')]
            try:
                clock[0] += .1
                yield {'type': 'delta', 'text': text}
                clock[0] += .1
                yield {'type': 'message', 'message': message, 'meta': {'usage': {
                    'prompt_tokens': 10, 'completion_tokens': 2, 'prompt_tokens_details': {'cached_tokens': 0}}}}
            finally:
                active = False

    async def sleep(seconds):
        assert not active
        waits.append(seconds)
        clock[0] += seconds + .125

    monkeypatch.setattr(harness, 'time', SimpleNamespace(perf_counter=lambda: clock[0]))
    monkeypatch.setattr(gateway_driver, 'time', SimpleNamespace(perf_counter=lambda: clock[0]))
    monkeypatch.setattr(harness, 'asyncio', SimpleNamespace(
        sleep=sleep, gather=asyncio.gather, Semaphore=asyncio.Semaphore, timeout=asyncio.timeout))
    spec = {'input': '请查一下公开资料', 'expect': {'model_calls': 2, 'route': 'lookup', 'no_commands': True}}
    cases = [{'id': 'two-turns', 'turns': [deepcopy(spec), deepcopy(spec)]},
             {'id': 'one-turn', 'turns': [deepcopy(spec)]}]
    for case in cases:
        case['lookup_fixtures'] = {'web_search': [{'ok': True, 'results': [{'title': '公开资料', 'url': 'https://example.org'}]}]}
    results = await harness.run_all(cases, Model(), concurrency=1, repeat=2,
                                    pause_seconds=3, max_first_useful_ms=500)
    turns = [turn for result in results for turn in result['turns']]
    assert len(requests) == 12 and len(turns) == 6 and len(waits) == 5
    assert waits == pytest.approx([3] * 5)
    assert all(len(request['tools']) == 26 for request in requests)
    assert all(result['passed'] and result['behavior_passed'] for result in results)
    assert turns[0]['actual_idle_ms'] is None and turns[0]['cadence_group'] == 'first_request'
    assert [turn['actual_idle_ms'] for turn in turns[1:]] == pytest.approx([3125] * 5)
    assert all(turn['cadence_group'] == 'after_pause' for turn in turns[1:])
    assert all(turn['first_effective_ms'] < turn['first_useful_ms'] for turn in turns)
    assert all(turn['first_useful_ms'] == pytest.approx(300) for turn in turns)
    assert all(requests[index + 1]['at'] - requests[index]['at'] == pytest.approx(.2)
               for index in range(0, 12, 2)), 'No artificial pause may enter tool continuation.'
    summary = harness.summarize(results)['latency']['all']
    assert summary['model_calls'] == summary['model_calls_by_role']['talker'] == 12
    assert summary['prompt_tokens'] == 120 and summary['first_useful_samples'] == 6
    assert summary['first_useful_gate_turns'] == 6 and summary['first_useful_gate_failed_turns'] == 0
    assert summary['cadence_groups']['after_pause']['turns'] == 5
    assert summary['cache_groups']['zero_cache']['first_useful_samples'] == 6
    assert all(result['traces'] for result in results)


async def test_original_failures_calls_and_slow_or_missing_samples_survive_gate(monkeypatch):
    samples = iter([
        {'first_useful_ms': 3000, 'model_calls': 3, 'model_calls_by_role': {'talker': 2, 'planner': 1}},
        {'first_useful_ms': None, 'model_calls': 1, 'model_calls_by_role': {'talker': 1}, 'error': 'timeout'},
    ])

    async def turn(self, *args):
        return {'commands': [], 'talker_calls': [], 'vehicle': {}, 'vehicle_before': {}, 'tasks_created': 0,
                'route': 'chat', 'first_effective_ms': 5, 'total_ms': 5000, **next(samples)}

    monkeypatch.setattr(gateway_driver.EvaluationGateway, 'turn', turn)
    case = {'id': 'retain-failures', 'turns': [
        {'input': '执行设置', 'expect': {'commands': [{'name': 'set_volume'}]}},
        {'input': '说明当前结果', 'expect': {'allow_error': True}},
    ]}
    results = await harness.run_all([case], object(), concurrency=1, max_first_useful_ms=2000)
    first, second = results[0]['turns']
    assert not results[0]['passed'] and not results[0]['behavior_passed']
    assert first['failures'][0].startswith('missing command') and len(first['failures']) == 2
    assert not first['behavior_passed'] and second['behavior_passed']
    assert not second['passed'] and len(second['latency_failures']) == 2
    assert second['actual_idle_ms'] >= 0 and second['cadence_group'] == 'consecutive'
    summary = harness.summarize(results)['latency']['all']
    assert summary['turns'] == summary['first_useful_gate_failed_turns'] == 2
    assert summary['first_useful_samples'] == summary['no_first_useful_turns'] == 1
    assert summary['first_useful_p90'] == 3000 and summary['total_p90'] == 5000
    assert summary['model_calls'] == 4 and summary['model_calls_by_role']['planner'] == 1


async def test_query_preface_and_real_lookup_receipt_do_not_pass_failed_continuation_gate():
    class Model:
        calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 2:
                raise ProviderError('timeout', 'offline continuation timeout', provider='fixture')
            text = '正在查询公开资料。'
            yield {'type': 'delta', 'text': text}
            yield {'type': 'message', 'message': {'role': 'assistant', 'content': text,
                'tool_calls': [call('web_search', {'query': '公开资料'})]}, 'meta': {'usage': {'prompt_tokens': 10}}}

    model = Model()
    case = {'id': 'query-continuation', 'lookup_fixtures': {'web_search': [{'ok': True, 'results': []}]},
            'turns': [{'input': '查询公开资料', 'expect': {'allow_error': True}}]}
    result = await harness.run_case(case, model, max_first_useful_ms=5000)
    turn = result['turns'][0]
    assert turn['first_effective_ms'] is not None and turn['first_effective_ms'] < 5000
    assert turn['preface'] and turn['presentation_source'] == 'error_template'
    assert turn['lookups'] == ['web_search'] and turn['lookup_results'][0]['result']['ok']
    assert turn['first_useful_ms'] is None and turn['error'] == 'timeout'
    assert model.calls == turn['model_calls'] == turn['talker_model_calls'] == 2
    assert not result['passed'] and not turn['passed']
    assert turn['failures'][0] == 'model errors observed: 1'
    assert len(turn['latency_failures']) == 2
    assert len([row for row in result['traces'] if row['kind'] == 'model.started']) == 2


@pytest.mark.parametrize('arguments', [
    ['--max-first-useful-ms', '0'], ['--max-first-useful-ms', '-1'],
    ['--max-first-useful-ms', 'nan'], ['--max-first-useful-ms', 'inf'],
    ['--pause-seconds', '-1'], ['--pause-seconds', 'nan'], ['--pause-seconds', 'inf'],
    ['--pause-seconds', '1'], ['--pause-seconds', '1', '--concurrency', '2'],
])
async def test_invalid_cli_gate_or_pause_is_rejected_before_provider_creation(monkeypatch, arguments):
    import super_xd.nlp.adapters.factory as factory
    monkeypatch.setattr(factory, 'create_llm', lambda: pytest.fail('Invalid options must not create a provider'))
    with pytest.raises(SystemExit) as error:
        await harness.main(arguments)
    assert error.value.code == 2


async def test_cli_records_gate_and_pause_and_returns_failure_without_dropping_results(monkeypatch, tmp_path):
    import super_xd.nlp.adapters.factory as factory
    closed, options = [], []

    async def close():
        closed.append(True)

    client = SimpleNamespace(config=SimpleNamespace(source='offline'), aclose=close,
        info=lambda: {'roles': {role: {'provider': {'configured': True}} for role in ('talker', 'planner')}})

    async def run(*args, **kwargs):
        options.append(kwargs)
        return [{'id': 'slow', 'persona': 'warm', 'repeat': 0, 'passed': False, 'behavior_passed': True,
                 'turns': [{'input': '查询资料', 'passed': False, 'failures': ['first-useful gate: too slow'],
                            'latency_failures': ['first-useful gate: too slow'], 'first_useful_ms': 3000,
                            'model_calls': 2, 'actual_idle_ms': None, 'cadence_group': 'first_request'}]}]

    monkeypatch.setattr(factory, 'create_llm', lambda: client)
    monkeypatch.setattr(harness, 'load_cases', lambda: [{'id': 'slow'}])
    monkeypatch.setattr(harness, 'run_all', run)
    destination = tmp_path / 'report.json'
    status = await harness.main(['--concurrency', '1', '--repeat', '3', '--pause-seconds', '30',
                                 '--max-first-useful-ms', '2000', '--out', str(destination)])
    assert status == 1 and closed == [True]
    assert options == [{'concurrency': 1, 'repeat': 3, 'max_first_useful_ms': 2000, 'pause_seconds': 30}]
    report = json.loads(destination.read_text())
    assert report['selection']['pause_seconds'] == 30 and report['selection']['max_first_useful_ms'] == 2000
    assert report['summary']['latency']['all']['model_calls'] == 2
    assert report['results'][0]['turns'][0]['first_useful_ms'] == 3000


async def test_pause_counts_existing_idle_without_adding_an_extra_full_pause(monkeypatch):
    clock = [100.0]

    async def unexpected_sleep(seconds):
        pytest.fail('Already elapsed setup/cleanup time satisfies the minimum idle interval')

    monkeypatch.setattr(harness, 'time', SimpleNamespace(perf_counter=lambda: clock[0]))
    monkeypatch.setattr(harness, 'asyncio', SimpleNamespace(sleep=unexpected_sleep))
    cadence = harness.SerialCadence(3)
    cadence.after_turn()
    clock[0] += 4
    assert await cadence.before_turn() == {'actual_idle_ms': 4000, 'cadence_group': 'after_pause'}
