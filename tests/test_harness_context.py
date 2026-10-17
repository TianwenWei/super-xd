"""Context contracts tested with plain records, without an actor or device."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from backend.harness.context import (
    ContextPrompts, ContextSelectionPolicy, ContextTurn, build_talker_messages,
)


PROMPTS = ContextPrompts('fixed rules', 'persona', 'current input',
                         'Snapshot for {{speaker_name}}\n{{snapshot_json}}')


def snapshot(tasks=()):
    return {
        'vehicle': {'climate': {'temperature': 23}, 'seats': {'memories': {'one': [1, 2]}, 'occupied': True}},
        'background_tasks_reference_only': list(tasks),
        'speaker': {'seat': 'driver', 'display_name': '主驾'},
        'recent_places': [{'name': '已核实地点'}],
        'cockpit': {'media': {'status': 'paused'}},
        'media_choices': [],
        'cockpit_capabilities': {'configured': False},
        'tool_capabilities': [{'name': 'set_volume', 'operation': 'control'}],
        'current_utc': '2026-10-17T12:00:00+00:00',
        'capabilities': {'gps': False},
    }


def user_turn(identifier, **options):
    return ContextTurn(identifier, [{'role': 'user', 'content': identifier}], **options)


def build(current, turns=(), state=None, **options):
    return build_talker_messages(current_turn=current, turns=turns, snapshot=state or snapshot(),
                                 prompts=PROMPTS, **options)


def read_snapshot(messages):
    return next(json.loads(message['content'].splitlines()[-1]) for message in messages
                if isinstance(message.get('content'), str) and message['content'].startswith('Snapshot for '))


@pytest.mark.parametrize('current_in_archive', [False, True])
def test_default_preserves_legacy_window_and_prompt_order(current_in_archive):
    current = user_turn('current')
    history = [user_turn(str(index)) for index in range(6)]
    if current_in_archive:
        history.append(current)
    messages = build(current, history)
    expected_history = history[-4:]
    if current_in_archive:
        expected_history = expected_history[:-1]
    expected_state = snapshot()
    del expected_state['vehicle']['seats']['memories']
    expected_state['interrupted_playback'] = []
    expected_state['speaker_changed_since_previous_turn'] = False
    assert messages == [
        {'role': 'system', 'content': 'fixed rules'},
        {'role': 'system', 'content': 'persona'},
        *[dict(turn.messages[0]) for turn in expected_history],
        {'role': 'system', 'content': 'Snapshot for 主驾\n' + json.dumps(expected_state, ensure_ascii=False, separators=(',', ':'))},
        {'role': 'system', 'content': 'current input'},
        {'role': 'user', 'content': 'current'},
    ]


def test_explicit_current_replaces_archived_record_without_changing_its_position():
    stale = ContextTurn('current', [{'role': 'user', 'content': '旧输入'}])
    current = ContextTurn('current', [
        {'role': 'user', 'content': '当前输入'},
        {'role': 'assistant', 'content': '完整但未播完'},
    ], interrupted_playback=True, heard_text='已听到')
    messages = build(current, [user_turn('before'), stale, user_turn('after')])
    assert [item['content'] for item in messages if item['role'] != 'system'] == [
        'before', '当前输入', '已听到', 'after',
    ]
    assert read_snapshot(messages)['interrupted_playback'] == [
        {'turn_id': 'current', 'heard_text': '已听到', 'status': 'interrupted'},
    ]
    assert stale.messages[0]['content'] == '旧输入'


def test_duplicate_current_records_produce_one_input_and_snapshot():
    current = user_turn('current')
    messages = build(current, [user_turn('before'), current, user_turn('current')])
    assert sum(item['content'] == 'current' for item in messages) == 1
    assert sum(item['content'] == PROMPTS.current_turn for item in messages) == 1
    assert sum(item['content'].startswith('Snapshot for ') for item in messages) == 1


def test_duplicate_historical_ids_fail_even_outside_selected_window():
    history = [user_turn('duplicate'), user_turn('duplicate'), *[user_turn(str(index)) for index in range(6)]]
    with pytest.raises(ValueError) as invalid:
        build(user_turn('current'), history)
    assert str(invalid.value) == 'Historical turn IDs must be unique'


def test_selection_of_active_tasks_is_explicit_and_default_keeps_last_eight():
    tasks = [{'id': str(index), 'status': 'completed', 'summary': f'状态{index}',
              'constraints': [f'完整条件{index}'],
              'result': {'summary': '完整报告' * 100, 'spoken_summary': f'结论{index}'}}
             for index in range(10)]
    tasks[0]['status'] = 'waiting_clarification'
    tasks[0]['question'] = '需要用户提供目的地'
    original = deepcopy(tasks)
    default = read_snapshot(build(user_turn('current'), state=snapshot(tasks)))['background_tasks_reference_only']
    assert [task['id'] for task in default] == [str(index) for index in range(2, 10)]
    assert [task['result'] for task in default] == [{'spoken_summary': f'结论{index}'} for index in range(2, 10)]
    preferred = read_snapshot(build(user_turn('current'), state=snapshot(tasks),
        policy=ContextSelectionPolicy(prefer_active_tasks=True)))['background_tasks_reference_only']
    assert [task['id'] for task in preferred] == ['0', *[str(index) for index in range(3, 10)]]
    assert preferred[0]['question'] == '需要用户提供目的地'
    assert preferred[0]['constraints'] == ['完整条件0']
    assert tasks == original


def test_user_turn_priority_keeps_user_context_after_many_proactive_notices():
    current = user_turn('current')
    history = [user_turn('older-user', speaker_seat='rear_left'),
               *[ContextTurn(f'notice-{index}', [{'role': 'assistant', 'content': f'notice-{index}'}], proactive=True)
                 for index in range(5)], current]
    default = build(current, history)
    assert not any(message.get('content') == 'older-user' for message in default)
    assert not read_snapshot(default)['speaker_changed_since_previous_turn']
    preferred = build(current, history, policy=ContextSelectionPolicy(prefer_user_turns=True))
    assert [message['content'] for message in preferred if message['role'] != 'system'] == [
        'older-user', 'notice-3', 'notice-4', 'current',
    ]
    assert read_snapshot(preferred)['speaker_changed_since_previous_turn']


def tool_call(identifier, arguments='{}'):
    return {'id': identifier, 'type': 'function', 'function': {'name': 'manage_task', 'arguments': arguments}}


def test_pending_tool_group_is_omitted_without_losing_earlier_complete_group():
    complete, pending, other = tool_call('complete'), tool_call('pending'), tool_call('other')
    records = [
        {'role': 'user', 'content': '保留所有条件'},
        {'role': 'assistant', 'content': None, 'tool_calls': [complete]},
        {'role': 'tool', 'tool_call_id': 'complete', 'content': '{"ok":true,"value":3}'},
        {'role': 'assistant', 'content': '第一项的真实结果'},
        {'role': 'assistant', 'content': None, 'tool_calls': [pending, other]},
        {'role': 'tool', 'tool_call_id': 'pending', 'content': '{"ok":true}'},
    ]
    current = user_turn('new input')
    previous = ContextTurn('previous', records, pending_tool_group=True)
    messages = build(current, [previous, current])
    assert messages[2:6] == records[:4]
    assert all(call['id'] == 'complete' for message in messages for call in message.get('tool_calls', []))
    assert records[4]['tool_calls'] == [pending, other]


def test_interrupted_speech_preserves_tool_messages_and_reports_only_heard_text():
    call = tool_call('complete')
    records = [
        {'role': 'user', 'content': '说明并查询'},
        {'role': 'assistant', 'content': '我去查询', 'tool_calls': [call]},
        {'role': 'tool', 'tool_call_id': 'complete', 'content': '{"ok":true}'},
        {'role': 'assistant', 'content': '完整生成但用户没有听完的回答'},
    ]
    previous = ContextTurn('previous', records, speaker_seat='rear_left', interrupted_playback=True, heard_text='实际听到部分')
    current = user_turn('current', speaker_seat='driver')
    messages = build(current, [previous, current])
    assert messages[3:5] == records[1:3]
    assert messages[5] == {'role': 'assistant', 'content': '实际听到部分'}
    assert read_snapshot(messages)['interrupted_playback'] == [
        {'turn_id': 'previous', 'heard_text': '实际听到部分', 'status': 'interrupted'},
    ]
    assert read_snapshot(messages)['speaker_changed_since_previous_turn']
    unheard = build(current, [replace(previous, heard_text=''), current])
    assert unheard[5]['content'] == ''
    assert records[-1]['content'] == '完整生成但用户没有听完的回答'


@pytest.mark.parametrize('arguments, detailed', [('{"read_details":true}', True), ('{}', False), ('not JSON', False), ('[]', False)])
def test_tool_result_compaction_uses_structured_call_arguments(arguments, detailed):
    result = {'summary': '完整报告', 'spoken_summary': '口头结论', 'sources': [{'url': 'https://example.test/fact'}]}
    records = [
        {'role': 'assistant', 'content': None, 'tool_calls': [tool_call('read', arguments)]},
        {'role': 'tool', 'tool_call_id': 'read', 'content': json.dumps({
            'state': {'duplicate': True}, 'task': {'id': 'task', 'status': 'completed', 'result': result},
        })},
    ]
    current = ContextTurn('current', records)
    messages = build(current, [current])
    actual = json.loads(messages[-1]['content'])
    assert 'state' not in actual
    assert actual['task']['result'] == (result if detailed else {'spoken_summary': '口头结论'})
    assert messages[-2] == records[0]
    assert messages[-1]['tool_call_id'] == 'read'


def test_builder_needs_no_file_io_and_never_mutates_inputs(monkeypatch):
    current = ContextTurn('current', [{'role': 'user', 'content': '精确保留', 'metadata': {'items': ['source']}}])
    state = snapshot()
    original_state, original_messages = deepcopy(state), deepcopy(current.messages)

    def no_file_io(*args, **kwargs):
        raise AssertionError('Context building must not read files')

    monkeypatch.setattr(Path, 'open', no_file_io)
    monkeypatch.setattr(Path, 'read_text', no_file_io)
    messages = build(current, [current], state)
    messages[-1]['metadata']['items'].append('output-only')
    assert current.messages == original_messages
    assert state == original_state
    assert build(current, [current], state)[-1] == original_messages[-1]


def test_zero_limits_keep_only_current_turn_and_invalid_limits_fail():
    policy = ContextSelectionPolicy(history_turn_limit=0, task_limit=0, prefer_user_turns=True, prefer_active_tasks=True)
    messages = build(user_turn('current'), [user_turn('previous')], snapshot([{'id': 'task', 'status': 'running'}]), policy=policy)
    assert len(messages) == 5
    assert messages[-1] == {'role': 'user', 'content': 'current'}
    assert read_snapshot(messages)['background_tasks_reference_only'] == []
    for invalid in (-1, True, 1.5):
        with pytest.raises(ValueError):
            ContextSelectionPolicy(history_turn_limit=invalid)


def test_snapshot_template_requires_exact_variable_contract():
    for invalid in ('{{snapshot_json}}', '{{speaker_name}} {{snapshot_json}} {{extra}}'):
        with pytest.raises(ValueError):
            ContextPrompts('core', 'persona', 'current', invalid)
