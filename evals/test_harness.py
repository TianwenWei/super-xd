"""Offline checks for the eval runner itself; live cases need a real model."""
from copy import deepcopy
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from evals import harness
from evals.harness import grade, load_cases, run_all, subset, summarize, persona_signature
from test_session import FakeArk, call
from vehicle_fakes import FakeVehiclePlanner


def observed(**overrides):
    base = {'commands': [], 'talker_calls': [], 'vehicle': {'a': {'b': 1}}, 'vehicle_before': {'a': {'b': 1}},
            'tasks_created': 0, 'route': 'chat', 'error': None}
    return {**base, **overrides}


def test_persona_signature_uses_number_value_but_keeps_boolean_and_strings_distinct():
    def signature(value):
        return persona_signature({'turns': [{'talker_calls': [{'name': 'set', 'arguments': {'value': value}}]}]})
    assert signature(24) == signature(24.0)
    assert signature(True) != signature(1)
    assert signature('24') != signature(24)


def test_cases_load_with_unique_ids_and_known_expectations():
    known = {'commands', 'commands_exact', 'no_commands', 'forbid_commands', 'talker_calls', 'talker_call_options',
             'forbid_talker_calls', 'forbid_lookups', 'no_talker_calls', 'state', 'unchanged', 'tasks_created', 'route', 'allow_error', 'manual_pages_any', 'cockpit_commands', 'cockpit_commands_exact', 'cockpit_state', 'model_calls'}
    cases = load_cases()
    assert len(cases) >= 20
    for case in cases:
        for turn in case['turns']:
            assert set(turn.get('expect', {})) <= known, case['id']


def test_subset_compares_structure_not_text():
    assert subset({'zone': 'driver'}, {'zone': 'driver', 'temperature': 23})
    assert subset(23, 23.0)
    assert not subset(True, 1)
    assert not subset({'zone': 'all'}, {'zone': 'driver'})


def test_accepted_tool_options_still_require_the_expected_city_date_and_tool():
    expect = {'talker_call_options': [[
        {'name': 'get_weather', 'arguments': {'location': 'Paris France', 'date': 'today'}},
        {'name': 'get_weather', 'arguments': {'location': 'Paris 法国', 'date': 'today'}},
    ]]}
    assert grade(expect, observed(talker_calls=[{'name': 'get_weather', 'arguments': {'location': 'Paris 法国', 'date': 'today'}}])) == []
    for item in [
        {'name': 'get_weather', 'arguments': {'location': 'Lyon France', 'date': 'today'}},
        {'name': 'get_weather', 'arguments': {'location': 'Paris France', 'date': 'tomorrow'}},
        {'name': 'web_search', 'arguments': {'location': 'Paris France', 'date': 'today'}},
    ]:
        assert grade(expect, observed(talker_calls=[item]))
    assert grade(expect, observed())


def test_grade_reports_missing_forbidden_and_state():
    expect = {'commands': [{'name': 'set_volume', 'arguments': {'volume': 35}}],
              'forbid_commands': [{'name': 'set_ambient_light'}], 'state': {'a.b': 2}, 'unchanged': ['a.b']}
    failures = grade(expect, observed(commands=[{'name': 'set_ambient_light', 'arguments': {'enabled': False}}]))
    assert len(failures) == 3
    good = observed(commands=[{'name': 'set_volume', 'arguments': {'volume': 35}}], vehicle={'a': {'b': 2}},
                    vehicle_before={'a': {'b': 2}})
    assert grade(expect, good) == []


async def test_runner_drives_simulated_device_and_checks_persona_invariance():
    case = {'id': 'volume', 'personas': ['professional', 'playful'],
            'turns': [{'input': '音量调到35', 'expect': {'commands': [{'name': 'set_volume', 'arguments': {'volume': 35}}],
                                                       'state': {'media.volume': 35}}}]}

    class PerRun:
        """Each run gets the same scripted decision, like a deterministic model."""
        def __init__(self):
            self.inner = None

        async def stream(self, messages, tools):
            async for item in FakeArk([{'tool_calls': [call('set_volume', {'volume': 35})]}]).stream(messages, tools):
                yield item

    results = await run_all([case], PerRun(), concurrency=2, planner=FakeVehiclePlanner())
    summary = summarize(results)
    assert {k: summary[k] for k in ('total', 'passed', 'pass_rate', 'failed')} == {'total': 2, 'passed': 2, 'pass_rate': 1.0, 'failed': {}}
    assert summary['latency']['all']['turns'] == 2
    assert results[0]['turns'][0]['commands'] == [{'name': 'set_volume', 'arguments': {'volume': 35}, 'source': 'talker'}]


def test_persona_signature_ignores_only_explicit_display_fields():
    def run(calls):
        return {'turns': [{'talker_calls': calls, 'commands': []}]}
    a = run([{'name': 'delegate_task', 'arguments': {'goal': '比较杭州的酒店', 'spoken_goal': '比较酒店'}}])
    b = run([{'name': 'delegate_task', 'arguments': {'goal': '比较杭州的酒店', 'spoken_goal': '查查杭州酒店'}}])
    c = run([{'name': 'set_temperature', 'arguments': {'zone': 'driver', 'temperature': 23}}])
    d = run([{'name': 'set_temperature', 'arguments': {'zone': 'all', 'temperature': 23}}])
    assert persona_signature(a) == persona_signature(b)
    assert persona_signature(c) != persona_signature(d)


@pytest.mark.parametrize('name, field, left, right', [
    ('web_search', 'query', 'Python正式版', 'Python预览版'),
    ('get_weather', 'location', 'Paris France', 'Lyon France'),
    ('get_weather', 'date', '2026-10-17', '2026-10-18'),
    ('search_places', 'area', '上海', '杭州'),
    ('set_navigation_destination', 'destination', '上海虹桥站', '上海站'),
    ('start_poi_navigation', 'place_id', 'place-one', 'place-two'),
    ('control_media', 'content_id', 'song-one', 'song-two'),
    ('delegate_task', 'goal', '比较杭州酒店', '比较上海酒店'),
    ('manage_task', 'task_id', 'task-one', 'task-two'),
    ('manage_task', 'task_ids', ['task-one'], ['task-two']),
    ('manage_task', 'text', '预算三百', '预算五百'),
    ('another_tool', 'spoken_goal', 'first', 'second'),
])
def test_persona_signature_preserves_business_strings(name, field, left, right):
    def run(value):
        return {'turns': [{'talker_calls': [{'name': name, 'arguments': {field: value}}]}]}
    assert persona_signature(run(left)) != persona_signature(run(right))


def test_task_id_normalization_preserves_logical_target_and_unknown_ids():
    def run(task_id, aliases):
        return {'task_id_aliases': aliases, 'turns': [{'talker_calls': [
            {'name': 'manage_task', 'arguments': {'action': 'reply', 'task_id': task_id, 'text': '杭州'}},
            {'name': 'manage_task', 'arguments': {'action': 'get', 'task_ids': [task_id]}},
        ]}]}
    first = run('random-a', {'random-a': 'seeded:0', 'random-b': 'seeded:1'})
    second = run('random-c', {'random-c': 'seeded:0', 'random-d': 'seeded:1'})
    other_target = run('random-d', second['task_id_aliases'])
    archive = deepcopy(first)
    assert persona_signature(first) == persona_signature(second)
    assert persona_signature(first) != persona_signature(other_target)
    assert persona_signature(first) != persona_signature(run('unknown', first['task_id_aliases']))
    assert persona_signature(first) != persona_signature(run('seeded:0', first['task_id_aliases']))
    assert first == archive


def test_task_id_aliases_do_not_normalize_unrelated_target_identifiers():
    def run(identifier):
        return {'task_id_aliases': {identifier: 'seeded:0'}, 'turns': [{'talker_calls': [
            {'name': 'start_poi_navigation', 'arguments': {'place_id': identifier}}]}]}
    assert persona_signature(run('first-place')) != persona_signature(run('second-place'))


async def test_runner_assigns_stable_task_aliases_without_mutating_shared_cases(monkeypatch):
    case = {'id': 'task-target', 'personas': ['professional', 'playful'],
            'tasks': [{'goal': '第一个任务'}, {'goal': '第二个任务'}],
            'turns': [{'input': '取消第一个任务', 'expect': {}}]}
    archive = deepcopy(case)

    async def drive(actor, outgoing, text):
        task_id = actor.task_list()[0]['id']
        return observed(talker_calls=[{'name': 'manage_task', 'arguments': {'action': 'cancel', 'task_id': task_id}}])

    monkeypatch.setattr(harness, '_drive_turn', drive)
    results = await run_all([case], FakeArk([]), repeat=2)
    assert case == archive
    assert all(result['passed'] for result in results)
    assert len({next(iter(result['task_id_aliases'])) for result in results}) == 4
    assert all(persona_signature(result) == persona_signature(results[0]) for result in results)


async def test_created_task_aliases_follow_creating_calls_despite_reversed_receipt_order():
    calls = [call('delegate_task', {'goal': 'first'}, id='call-first'),
             call('delegate_task', {'goal': 'second'}, id='call-second')]
    turn = SimpleNamespace(id='turn', messages=[{'role': 'assistant', 'tool_calls': calls}], tool_results={
        'call-second': {'ok': True, 'task': {'id': 'task-second'}},
        'call-first': {'ok': True, 'task': {'id': 'task-first'}},
    })

    async def post(event):
        pass

    actor = SimpleNamespace(vehicle={}, post=post, turns=[turn], cockpit=None, cockpit_state={})
    outgoing = asyncio.Queue()
    for event in [
        {'type': 'turn.started', 'turn_id': 'turn'},
        {'type': 'ui.card', 'origin_turn_id': 'turn', 'task': {'id': 'task-second'}},
        {'type': 'ui.card', 'origin_turn_id': 'turn', 'task': {'id': 'task-first'}},
        {'type': 'stream.done', 'turn_id': 'turn', 'metrics': {}, 'text': ''},
    ]:
        outgoing.put_nowait(event)
    result = await harness._drive_turn(actor, outgoing, '两项调研')
    assert result['created_task_indices'] == {'task-first': 0, 'task-second': 1}


def persona_result(persona, repeat, goal, *, passed=True):
    return {'id': 'research', 'persona': persona, 'repeat': repeat, 'passed': passed, 'turns': [
        {'input': '比较酒店', 'passed': passed, 'failures': [] if passed else ['wrong goal'],
         'talker_calls': [{'name': 'delegate_task', 'arguments': {'goal': goal}}]}]}


async def test_persona_metrics_show_variation_even_when_shared_sequence_gate_passes(monkeypatch):
    async def run(case, ark, *, persona, repeat, **kwargs):
        goal = 'shared' if repeat == 0 else persona
        return persona_result(persona, repeat, goal)

    monkeypatch.setattr(harness, 'run_case', run)
    results = await run_all([{'id': 'research', 'personas': ['warm', 'playful']}], None, repeat=3)
    assert all(result['passed'] for result in results)
    metrics = summarize(results)['persona_metrics']
    case = metrics['cases']['research']
    assert case['compatibility_passed'] is True
    assert case['shared_sequence_count'] == 1
    assert case['all_runs_identical'] is False
    assert case['cross_persona_pairs'] == 9
    assert case['different_cross_persona_pairs'] == 8
    assert case['cross_persona_difference_rate'] == round(8 / 9, 4)
    assert metrics['cross_persona_difference_rate'] == case['cross_persona_difference_rate']
    assert case['per_persona']['warm']['distinct_sequences'] == 2
    assert metrics['per_persona']['warm']['pass_rate'] == 1


async def test_persona_metrics_separate_behavior_passes_from_consistency_gate(monkeypatch):
    async def run(case, ark, *, persona, repeat, **kwargs):
        return persona_result(persona, repeat, persona)

    monkeypatch.setattr(harness, 'run_case', run)
    results = await run_all([{'id': 'research', 'personas': ['warm', 'playful']}], None)
    assert not any(result['passed'] for result in results)
    metrics = summarize(results)['persona_metrics']
    assert metrics['cases']['research']['compatibility_passed'] is False
    assert metrics['cross_persona_difference_rate'] == 1
    assert metrics['per_persona']['warm']['behavior_pass_rate'] == 1
    assert metrics['per_persona']['warm']['pass_rate'] == 0
    assert 'shared by all personas' in results[0]['failures'][0]


def test_persona_metrics_do_not_invent_cross_persona_pairs_for_single_persona_or_empty_results():
    summary = summarize([persona_result('warm', 0, 'first'), persona_result('warm', 1, 'second', passed=False)])
    metrics = summary['persona_metrics']
    assert metrics['cross_persona_pairs'] == 0
    assert metrics['cross_persona_difference_rate'] is None
    assert metrics['cases']['research']['all_runs_identical'] is False
    assert metrics['per_persona']['warm']['pass_rate'] == .5
    assert summarize([])['persona_metrics']['cases'] == {}
    assert summarize([])['latency']['all']['model_calls_mean'] is None


@pytest.mark.parametrize('passed, expected_status', [(True, 0), (False, 1)])
async def test_cli_returns_failure_status_after_writing_report(monkeypatch, tmp_path, passed, expected_status):
    from backend.services import factory

    class OfflineClient:
        def __init__(self):
            self.config = SimpleNamespace(source='explicit-offline-service-config')
            self.closed = False

        def info(self):
            return {'roles': {role: {'provider': {'provider': 'ark', 'configured': True}, 'model': 'offline-' + role}
                              for role in ('talker', 'planner', 'thinker', 'finalizer')}}

        async def aclose(self):
            self.closed = True

    client = OfflineClient()

    async def run(*args, **kwargs):
        return [persona_result('warm', 0, 'task', passed=passed)]

    monkeypatch.delenv('ARK_API_KEY', raising=False)
    monkeypatch.setattr(factory, 'create_llm', lambda: client)
    monkeypatch.setattr(harness, 'load_cases', lambda: [{'id': 'research'}])
    monkeypatch.setattr(harness, 'run_all', run)
    destination = tmp_path / 'report.json'
    assert await harness.main(['--out', str(destination)]) == expected_status
    assert client.closed is True
    report = json.loads(destination.read_text())
    assert report['release'] == harness.get_release_manifest()
    assert report['model_configuration'] == {'source': client.config.source, **client.info()}
    assert report['summary']['passed'] == int(passed)
    assert report['results'][0]['passed'] is passed


@pytest.mark.parametrize('arguments', [['--case', 'missing'], ['--tag', 'missing']])
async def test_cli_rejects_empty_selection_before_creating_client(monkeypatch, capsys, arguments):
    from backend.services import factory

    def unexpected_client():
        pytest.fail('An empty selection must not construct a provider client')

    monkeypatch.delenv('ARK_API_KEY', raising=False)
    monkeypatch.setattr(factory, 'create_llm', unexpected_client)
    monkeypatch.setattr(harness, 'load_cases', lambda: [{'id': 'research', 'tags': ['task']}])
    with pytest.raises(SystemExit) as error:
        await harness.main(arguments)
    assert error.value.code == 2
    assert 'No eval cases matched' in capsys.readouterr().err


@pytest.mark.parametrize('arguments', [['--repeat', '0'], ['--repeat', '-1'], ['--concurrency', '0']])
async def test_cli_rejects_nonpositive_run_counts(arguments):
    with pytest.raises(SystemExit) as error:
        await harness.main(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize('configured', [True, False])
async def test_cli_uses_selected_config_custom_key_and_model_not_legacy_ark_env(monkeypatch, tmp_path, configured):
    from backend.services import config, llm
    from backend.settings import ROOT

    selected = tmp_path / 'selected-services.toml'
    selected.write_text((ROOT / 'config/services.toml').read_text().replace(
        'key_env = "ARK_API_KEY"', 'key_env = "EVAL_CONFIGURED_KEY"').replace(
        'model_env = "TALKER_MODEL"', 'model_env = "EVAL_CONFIGURED_TALKER_MODEL"'))
    monkeypatch.setenv('SERVICES_CONFIG_PATH', str(selected))
    monkeypatch.setenv('EVAL_CONFIGURED_TALKER_MODEL', 'configured-eval-model')
    monkeypatch.delenv('ARK_API_KEY', raising=False)
    if configured:
        monkeypatch.setenv('EVAL_CONFIGURED_KEY', 'offline-custom-account-secret')
    else:
        monkeypatch.delenv('EVAL_CONFIGURED_KEY', raising=False)
    providers, calls = [], []

    class OfflineProvider:
        def __init__(self, profile):
            self.profile, self.closed = profile, False
            providers.append(self)

        def info(self):
            return {'provider': self.profile['provider'], 'configured': bool(os.getenv(self.profile['key_env'])),
                    'capabilities': {'tools': True, 'tool_choice': True}}

        async def aclose(self):
            self.closed = True

    async def run(cases, client, **options):
        calls.append(client)
        assert client.request([]).model == 'configured-eval-model'
        assert client.providers['ark'].profile['key_env'] == 'EVAL_CONFIGURED_KEY'
        return [persona_result('warm', 0, 'task')]

    monkeypatch.setattr(llm, 'ChatProvider', OfflineProvider)
    monkeypatch.setattr(harness, 'load_cases', lambda: [{'id': 'research'}])
    monkeypatch.setattr(harness, 'run_all', run)
    config.load_services_config.cache_clear()
    destination = tmp_path / 'report.json'
    try:
        if configured:
            assert await harness.main(['--out', str(destination)]) == 0
            report_text = destination.read_text()
            report = json.loads(report_text)
            assert report['model_configuration']['source'] == str(selected)
            assert report['model_configuration']['roles']['talker']['model'] == 'configured-eval-model'
            assert 'offline-custom-account-secret' not in report_text
            assert len(calls) == 1
        else:
            with pytest.raises(SystemExit) as error:
                await harness.main(['--out', str(destination)])
            assert error.value.code == 2 and not calls and not destination.exists()
        assert providers and all(provider.closed for provider in providers)
    finally:
        config.load_services_config.cache_clear()


async def test_cli_closes_all_role_providers_when_evaluation_raises(monkeypatch):
    from backend.services import factory

    closed = []
    async def close():
        closed.append(True)
    client = SimpleNamespace(config=SimpleNamespace(source='offline'), aclose=close,
        info=lambda: {'roles': {role: {'provider': {'configured': True}} for role in ('talker', 'planner')}})
    async def failed(*args, **options):
        raise RuntimeError('offline evaluation failure')
    monkeypatch.setattr(factory, 'create_llm', lambda: client)
    monkeypatch.setattr(harness, 'load_cases', lambda: [{'id': 'research'}])
    monkeypatch.setattr(harness, 'run_all', failed)
    with pytest.raises(RuntimeError, match='offline evaluation failure'):
        await harness.main([])
    assert closed == [True]


async def test_case_report_retains_paginated_trace_after_actor_close(monkeypatch):
    class FlushEvidenceActor(harness.SessionActor):
        async def close(self):
            await super().close()
            def append_after_flush():
                with self.db.connection() as connection:
                    for index in range(501):
                        self.db._append_trace(connection, self.id, 'fixture.after_close', {'index': index})
            await asyncio.to_thread(append_after_flush)
    monkeypatch.setattr(harness, 'SessionActor', FlushEvidenceActor)
    model = FakeArk([{'content': '这就调整音量。', 'tool_calls': [call('set_volume', {'volume': 35})]}])
    case = {'id': 'trace-control', 'turns': [{'input': '把音量调到35', 'expect': {
        'commands': [{'name': 'set_volume', 'arguments': {'volume': 35}}], 'commands_exact': True}}]}
    result = await harness.run_case(case, model, planner=FakeVehiclePlanner())
    assert result['passed']
    records = result['traces']
    assert [row['trace_id'] for row in records] == sorted({row['trace_id'] for row in records})
    assert len([row for row in records if row['kind'] == 'fixture.after_close']) == 501
    assert records[-1]['data'] == {'index': 500}
    kinds = [row['kind'] for row in records]
    for kind in ('turn.started', 'model.started', 'model.completed', 'vehicle.dispatched',
                 'vehicle.receipt', 'tool.completed', 'turn.completed'):
        assert kind in kinds
    assert result['trace_capture'] == {'after_actor_close': True, 'event_count': len(records), 'recording_error': None}
    assert result['turns'][0]['model_errors'] == []
    started = next(row for row in records if row['kind'] == 'model.started')
    assert started['data']['messages'][-1]['content'].endswith('把音量调到35')
    assert started['turn_id'] == result['turns'][0]['turn_id']


async def test_case_error_trace_retains_provider_cause_partial_and_redacts_custom_key():
    from backend.services.contracts import ProviderError
    secret = 'offline-custom-model-secret'
    error = ProviderError('connection_error', 'ProviderError', provider='ark', retryable=True,
        details={'status_code': None, 'exception_type': 'ConnectTimeout',
                 'diagnostic': 'connection attempt failed: ' + secret,
                 'media': {'kind': 'media', 'source_url': 'https://media.test/audio?token=fixture-capability'}})
    error.trace_partial = {'message': {'role': 'assistant', 'content': None},
        'meta': {'response_headers_ms': None, 'elapsed_ms': 20508,
                 'actual_request': {'model': 'configured-model', 'messages': [{'role': 'user', 'content': '输入'}]},
                 'raw_response': {'error': {'code': 'connection_error', 'message': secret}}}}
    model = FakeArk([error])
    model.providers = {'custom': SimpleNamespace(secrets=(secret,))}
    result = await harness.run_case({'id': 'trace-model-error', 'turns': [{'input': '查一下信息', 'expect': {}}]},
                                    model, planner=FakeVehiclePlanner())
    assert not result['passed']
    records = result['turns'][0]['model_errors']
    assert len(records) == 1 and records[0]['kind'] == 'model.error'
    data = records[0]['data']
    assert data['provider_error']['code'] == 'connection_error'
    assert data['diagnostics']['exception_type'] == 'ConnectTimeout'
    assert data['partial']['meta']['elapsed_ms'] == 20508
    assert data['partial']['meta']['actual_request']['model'] == 'configured-model'
    serialized = json.dumps(result)
    assert secret not in serialized and 'fixture-capability' not in serialized
    assert 'model.started' in [row['kind'] for row in result['traces']]
    assert 'turn.completed' in [row['kind'] for row in result['traces']]


async def test_mixed_vehicle_and_real_player_fixture_keeps_three_actions_and_one_decision():
    case=next(case for case in load_cases() if case['id']=='multi_three_hot_actions')
    model=FakeArk([{'content':'这就调整。','tool_calls':[
        call('control_media',{'action':'pause'},id='media'),
        call('set_temperature',{'zone':'rear','temperature':25},id='temperature'),
        call('set_seat_heating',{'seat':'driver','level':1},id='seat')]}])
    result=(await run_all([case],model,concurrency=1,planner=FakeVehiclePlanner()))[0]
    assert result['passed'] is True
    turn=result['turns'][0]
    assert len(turn['commands'])==2 and len(turn['cockpit_commands'])==1
    assert turn['model_calls']==1 and len(model.requests)==1
    assert turn['cockpit_device_mode']=='explicit_eval_fixture'
    assert grade({'model_calls':1},observed(model_calls=2))
