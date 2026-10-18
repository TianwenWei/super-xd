"""The offline request capture must accept the checked-in synthetic task cases."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from scripts import validate_structured_prompts as capture_script


@pytest.fixture
def runtime(monkeypatch):
    runtime = capture_script.import_runtime()

    async def unexpected_provider_call(*args, **kwargs):
        pytest.fail('offline capture invoked a real model client')

    monkeypatch.setattr(runtime.models.ArkTransport, 'stream', unexpected_provider_call)
    monkeypatch.setattr(runtime.models.ArkTransport, 'complete', unexpected_provider_call)
    return runtime


async def test_full_synthetic_suite_captures_offline(runtime, tmp_path, monkeypatch):
    suite_path = capture_script.ROOT / 'evals' / 'structured_prompt_cases.json'
    suite = json.loads(suite_path.read_text())
    output = tmp_path / 'capture.json'
    monkeypatch.setattr(capture_script, 'import_runtime', lambda: runtime)
    await capture_script.capture(SimpleNamespace(cases=str(suite_path), output=str(output)))
    result = json.loads(output.read_text())
    assert [case['id'] for case in result['cases']] == [case['id'] for case in suite['cases']]
    assert result['provider_calls'] == 0
    assert result['tool_execution'] is False
    for case in result['cases']:
        assert case['request']['messages']
        assert case['request_sha256'] == capture_script.digest(case['request'])
        assert case['tools_sha256'] == capture_script.digest(case['request']['tools'])


async def test_task_fixtures_use_suite_time_without_mutating_cases(runtime, monkeypatch):
    suite = json.loads((capture_script.ROOT / 'evals' / 'structured_prompt_cases.json').read_text())
    case = deepcopy(next(case for case in suite['cases'] if case['id'] == 'task_get_multiple'))
    explicit_time = '2026-10-13T10:00:00+00:00'
    case['tasks'][0]['created_at'] = explicit_time
    original = deepcopy(case)
    request, _ = capture_script.capture_start_request(runtime, case, suite['fixed_current_utc'])
    assert request.received_at == suite['fixed_current_utc']
    assert request.context.observed_at == suite['fixed_current_utc']
    assert [task.id for task in request.context.tasks] == ['task_hotel', 'task_museum']
    first = await capture_script.capture_case(runtime, case, suite['fixed_current_utc'])
    second = await capture_script.capture_case(runtime, case, suite['fixed_current_utc'])
    assert case == original
    assert first == second


def comparison_fixture():
    examples = [{'role': 'user', 'content': 'native example'}, {'role': 'assistant', 'content': 'example reply'}]
    capture = {'fixed_current_utc': '2026-10-14T12:00:00+00:00', 'cases': [{
        'id': 'fixture', 'role': 'talker', 'text': 'current question', 'request': {
            'model': 'offline-model', 'temperature': 0, 'tools': [],
            'messages': [{'role': 'system', 'content': 'long fixed rules'},
                         {'role': 'system', 'content': 'current persona'}, *deepcopy(examples),
                         {'role': 'user', 'content': 'current question'}],
        },
    }]}
    return capture, examples


def test_capture_comparison_measures_text_changes_and_retains_examples():
    before, examples = comparison_fixture()
    after = deepcopy(before)
    after['cases'][0]['request']['messages'][0]['content'] = 'rules'
    report = capture_script.compare_captures(before, after, examples)
    assert report['by_role']['talker']['delta'] == {
        'messages': 0, 'content_characters': -11, 'system_characters': -11, 'serialized_characters': -11}
    assert report['native_examples_preserved'] is True
    assert report['native_examples_checked_cases'] == 1
    assert report['tools_identical'] and report['model_options_identical']
    assert report['non_system_messages_identical'] and report['case_data_identical']
    assert report['provider_calls'] == 0


def test_capture_comparison_exposes_contract_changes(tmp_path):
    before, examples = comparison_fixture()
    after = deepcopy(before)
    after['cases'][0]['text'] = 'different fixture'
    request = after['cases'][0]['request']
    request['model'] = 'different-model'
    request['tools'] = [{'different': 'tool'}]
    request['messages'][2]['content'] = 'changed example'
    report = capture_script.compare_captures(before, after, examples)
    assert not report['model_options_identical']
    assert not report['tools_identical']
    assert not report['case_data_identical']
    assert not report['non_system_messages_identical']
    assert not report['native_examples_preserved']


@pytest.mark.parametrize('change', ['clock', 'case_id', 'empty', 'duplicate'])
def test_capture_comparison_rejects_incomparable_fixtures(change):
    before, examples = comparison_fixture()
    after = deepcopy(before)
    if change == 'clock':
        after['fixed_current_utc'] = '2026-10-15T12:00:00+00:00'
    elif change == 'case_id':
        after['cases'][0]['id'] = 'different-case'
    elif change == 'empty':
        before['cases'] = after['cases'] = []
    else:
        before['cases'] *= 2
        after['cases'] *= 2
    with pytest.raises(ValueError):
        capture_script.compare_captures(before, after, examples)
