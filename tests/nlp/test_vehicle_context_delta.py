"""Receipt snapshots may shrink, but every actual fact must reconstruct exactly."""
from copy import deepcopy
import hashlib
import json

import pytest

from super_xd.nlp.prompts.model_context import compact_message, compact_tool_result


def baseline():
    return {'revision': 7, 'observed_at': '2026-10-17T12:00:00+00:00', 'state': {
        '_meta': {'timezone': 'Asia/Shanghai'},
        'climate': {'power': False, 'temperature': 20},
        'windows': {'positions': {'driver': 50, 'rear_right': 10}},
        'unrelated': {'facts': [{'id': index, 'condition': '真实设备事实，保留完整值'} for index in range(80)]},
    }}


def receipt(state):
    return {'ok': True, 'executed': True, 'name': 'set_climate_power', 'arguments': {'enabled': True},
            'command_id': 'stable-command', 'receipt_id': 'receipt-8', 'state_version': 8,
            'observed_at': '2026-10-17T12:00:01+00:00', 'vehicle_state': state}


def reconstruct(base, delta):
    encoded = json.dumps(base['state'], ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    assert delta['baseline'] == {'revision': base['revision'], 'observed_at': base['observed_at'],
                                 'state_sha256': hashlib.sha256(encoded.encode()).hexdigest()}
    result = deepcopy(base['state'])
    for path in delta['remove']:
        parent = result
        for part in path[:-1]:
            parent = parent[part]
        del parent[path[-1]]
    for change in delta['set']:
        parent = result
        for part in change['path'][:-1]:
            parent = parent[part]
        parent[change['path'][-1]] = deepcopy(change['value'])
    return result


def test_delta_preserves_unrelated_changes_and_original_wire_and_baseline():
    base = baseline()
    state = deepcopy(base['state'])
    state['climate']['power'] = True
    state['windows']['positions']['rear_right'] = 0  # A concurrent device fact, not this tool's declared effect.
    state['new_module'] = {'ready': None, 'values': [3, 2, 1]}
    del state['climate']['temperature']
    body = receipt(state)
    archived, original_base = deepcopy(body), deepcopy(base)
    output = compact_tool_result(body, 'set_climate_power', vehicle_baseline=base)
    delta = output['vehicle_state_delta']
    assert reconstruct(base, delta) == state
    assert 'vehicle_state' not in output
    assert {k: v for k, v in output.items() if k != 'vehicle_state_delta'} == {
        k: v for k, v in body.items() if k != 'vehicle_state'}
    assert body == archived and base == original_base
    delta['set'][0]['value'] = 'changed output only'
    assert body == archived and base == original_base


@pytest.mark.parametrize('later_baseline', [False, True])
def test_old_receipt_and_new_continuation_are_both_scoped_to_receipt_time(later_baseline):
    base = baseline()
    state = deepcopy(base['state'])
    state['climate']['power'] = True
    body = receipt(state)
    if later_baseline:
        base['revision'] = 10
        base['observed_at'] = '2026-10-17T12:05:00+00:00'
        base['state']['windows']['positions']['driver'] = 0
    output = compact_tool_result(body, vehicle_baseline=base)
    delta = output['vehicle_state_delta']
    assert output['state_version'] == 8 and output['receipt_id'] == 'receipt-8'
    assert delta['receipt_identity']['observed_at'] == body['observed_at']
    assert '历史回执不是本轮当前状态' in delta['receipt_time_scope']
    assert reconstruct(base, delta) == body['vehicle_state']


@pytest.mark.parametrize('outcome', [
    {'ok': False, 'executed': False, 'reason': '联动条件不满足', 'resume_condition': '等待真实核对'},
    {'ok': False, 'executed': None, 'status': 'unknown', 'error_code': 'receipt_unknown'},
    {'ok': False, 'executed': True, 'changed': ['climate.power'], 'applied': [{'power': True}],
     'rejected': [{'field': 'temperature', 'reason': 'out_of_range'}]},
])
def test_failures_unknown_and_partial_execution_fields_survive_without_success_inference(outcome):
    base = baseline()
    body = {**receipt(deepcopy(base['state'])), **outcome}
    output = compact_tool_result(body, vehicle_baseline=base)
    for key, value in outcome.items():
        assert output[key] == value
    assert reconstruct(base, output['vehicle_state_delta']) == body['vehicle_state']


def test_missing_baseline_or_small_snapshot_or_preexisting_delta_is_not_rewritten():
    base = baseline()
    body = receipt(deepcopy(base['state']))
    assert compact_tool_result(body) == body
    small = receipt({'power': True})
    small_base = {**base, 'state': {'power': False}}
    assert compact_tool_result(small, vehicle_baseline=small_base) == small
    body['vehicle_state_delta'] = {'unrecognized': 'provider fact must not be overwritten'}
    assert compact_tool_result(body, vehicle_baseline=base) == body


def test_shape_type_array_null_and_omitted_facts_are_lossless():
    base = baseline()
    base['state']['variants'] = {'bool': True, 'number': 1, 'removed': 'old', 'list': [1, 2],
                                  'nested': {'old': 1}, 'empty': {}, 'typed_list': [True, {'x': 1}],
                                  'literal.dot': {'/slash': 'before'}}
    state = deepcopy(base['state'])
    state['variants'] = {'bool': 1, 'number': 1.0, 'list': [2, 1], 'nested': None,
                         'empty': {'new': []}, 'added_null': None, 'typed_list': [1, {'x': 1.0}],
                         'literal.dot': {'/slash': 'after'}}
    output = compact_tool_result(receipt(state), vehicle_baseline=base)
    restored = reconstruct(base, output['vehicle_state_delta'])
    assert json.dumps(restored, sort_keys=True) == json.dumps(state, sort_keys=True)


def test_message_protocol_and_nested_batch_receipt_identity_remain_separate():
    base = baseline()
    first = receipt(deepcopy(base['state']))
    second = receipt(deepcopy(base['state']))
    second.update(receipt_id='receipt-9', state_version=9)
    second['vehicle_state']['climate']['power'] = True
    body = {'items': [first, second], 'reason': '完整批次说明'}
    message = {'role': 'tool', 'tool_call_id': 'call-real', 'content': json.dumps(body, ensure_ascii=False)}
    archived = deepcopy(message)
    result = compact_message(message, vehicle_baseline=base)
    assert result['role'] == 'tool' and result['tool_call_id'] == 'call-real'
    compact = json.loads(result['content'])
    for original, item in zip(body['items'], compact['items']):
        assert reconstruct(base, item['vehicle_state_delta']) == original['vehicle_state']
        assert item['vehicle_state_delta']['receipt_identity']['receipt_id'] == original['receipt_id']
    assert message == archived


@pytest.mark.parametrize('invalid', [
    {}, {'state': []}, {'state': {}, 'revision': True, 'observed_at': 'time'},
    {'state': {}, 'revision': 1, 'observed_at': None},
])
def test_unidentifiable_baselines_keep_full_snapshot_without_inventing_metadata(invalid):
    body = receipt(baseline()['state'])
    assert compact_tool_result(body, vehicle_baseline=invalid) == body
