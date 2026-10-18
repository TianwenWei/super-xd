"""Context contracts tested with plain records, without an actor or device."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from super_xd.nlp.prompts.context import ContextPrompts, ContextSelectionPolicy, ContextTurn, build_talker_messages, project_talker_snapshot


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
    expected_state['playback_history'] = []
    expected_state['speaker_changed_since_previous_turn'] = False
    expected_state = {'speaker': expected_state.pop('speaker'), 'speaker_changed_since_previous_turn': False,
                      **expected_state}
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
        'before', '当前输入', '完整但未播完', 'after',
    ]
    assert read_snapshot(messages)['playback_history'] == [
        {'turn_id': 'current', 'heard_text': '已听到', 'status': 'partial', 'interrupted': True},
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


def test_current_speaker_and_change_flag_precede_state_without_rewriting_history():
    state = snapshot()
    state['speaker'] = {'seat': 'front_passenger', 'display_name': '副驾'}
    previous = user_turn('历史原话', speaker_seat='rear_right')
    original = deepcopy(state)
    messages = build(user_turn('当前原话', speaker_seat='front_passenger'), [previous], state=state)
    actual = read_snapshot(messages)
    assert list(actual)[:2] == ['speaker', 'speaker_changed_since_previous_turn']
    assert actual['speaker'] == original['speaker'] and actual['speaker_changed_since_previous_turn'] is True
    assert messages[2] == previous.messages[0]
    assert messages[-1]['content'] == '当前原话'
    assert state == original


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


def test_interrupted_speech_preserves_generated_answer_and_separate_delivery_facts():
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
    assert messages[5] == records[3]
    assert read_snapshot(messages)['playback_history'] == [
        {'turn_id': 'previous', 'heard_text': '实际听到部分', 'status': 'partial', 'interrupted': True},
    ]
    assert read_snapshot(messages)['speaker_changed_since_previous_turn']
    unheard = build(current, [replace(previous, heard_text=''), current])
    assert unheard[5]['content'] == records[3]['content']
    assert read_snapshot(unheard)['playback_history'][0]['status'] == 'unknown'
    assert records[-1]['content'] == '完整生成但用户没有听完的回答'


@pytest.mark.parametrize('status', ['unknown', 'not_started', 'partial', 'completed'])
def test_delivery_status_never_replaces_any_generated_message(status):
    records = [
        {'role': 'user', 'content': '查询开闭馆日期'},
        {'role': 'assistant', 'content': '查询预告'},
        {'role': 'assistant', 'content': '周一闭馆，其他日期需看安排'},
    ]
    prior = ContextTurn('prior', records, playback_status=status)
    messages = build(user_turn('追问'), [prior])
    assert messages[2:5] == records
    delivery = read_snapshot(messages)['playback_history']
    assert delivery == [{'turn_id': 'prior', 'heard_text': '', 'status': status, 'interrupted': False}]


def test_unknown_delivery_and_invalid_delivery_are_distinct():
    with pytest.raises(ValueError, match='Unstarted playback'):
        user_turn('x', playback_status='not_started', heard_text='已经听见')
    with pytest.raises(ValueError, match='Invalid playback'):
        user_turn('x', playback_status='guessed')


def test_snapshot_compaction_preserves_capabilities_and_default_targets():
    state = snapshot()
    state['speaker']['default_targets'] = {'seat': 'rear_left'}
    state['speaker']['control_defaults'] = {
        'set_seat_heating': {'tool': 'set_seat_heating', 'skill_id': 'seats', 'arguments': {'seat': 'rear_left'}},
        'fold_mirror': {'tool': 'delegate_vehicle_control', 'skill_id': 'mirrors', 'arguments': {'side': 'left'}},
    }
    state['tool_capabilities'] = [
        {'name': 'control_media', 'action': 'pause', 'content_kind': None, 'available': True,
         'operation': 'control', 'execution_location': 'device', 'domain': 'media'},
        {'name': 'search_places', 'action': None, 'content_kind': None, 'available': False,
         'operation': 'query', 'execution_location': 'cloud', 'domain': 'places'},
    ]
    original = deepcopy(state)
    actual = read_snapshot(build(user_turn('current'), state=state))
    assert state == original
    assert actual['speaker']['default_targets'] == original['speaker']['default_targets']
    assert actual['speaker']['control_defaults']['set_seat_heating'] == {
        'skill_id': 'seats', 'arguments': {'seat': 'rear_left'}}
    assert actual['speaker']['control_defaults']['fold_mirror'] == original['speaker']['control_defaults']['fold_mirror']
    for old, compact in zip(original['tool_capabilities'], actual['tool_capabilities']):
        assert compact['name'] == old['name']
        assert compact.get('available', True) == old['available']
        assert compact.get('action') == old['action']
        assert compact.get('content_kind') == old['content_kind']


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


def test_fixed_talker_projection_preserves_denials_and_missing_cold_capabilities():
    state = snapshot()
    state['capabilities'] = {'gps': False, 'warm_seat': True, 'fold_mirror': False,
                             'open_shade': True, 'set_volume': False}
    state['tool_capabilities'] = [
        {'name': 'set_volume', 'available': False, 'operation': 'control', 'execution_location': 'device'},
        {'name': 'control_media', 'action': 'pause', 'available': True, 'operation': 'control', 'execution_location': 'device'},
        {'name': 'control_media', 'action': 'play', 'available': False, 'operation': 'control', 'execution_location': 'device'},
        {'name': 'warm_seat', 'available': True, 'operation': 'control', 'execution_location': 'device'},
        {'name': 'fold_mirror', 'available': True, 'operation': 'control', 'execution_location': 'device'},
        {'name': 'seat_status', 'available': True, 'operation': 'query', 'execution_location': 'cloud'},
        {'name': 'invented_control', 'available': True, 'operation': 'control', 'execution_location': 'device'},
    ]
    catalog = {'warm_seat': ('seats', 'control'), 'seat_status': ('seats', 'query'),
               'fold_mirror': ('mirrors', 'control'), 'open_shade': ('windows', 'control')}
    original = deepcopy(state)
    projected = project_talker_snapshot(state, {'set_volume', 'control_media'}, catalog)
    assert projected['tool_capabilities'] == original['tool_capabilities'][:3]
    assert projected['capabilities'] == {'gps': False, 'set_volume': False}
    assert projected['vehicle_skill_capabilities'] == {
        'seats': {'available_count': 1, 'available': ['warm_seat'], 'unavailable': ['seat_status']},
        'mirrors': {'available_count': 0, 'unavailable': ['fold_mirror']},
        'windows': {'available_count': 0, 'missing': ['open_shade']},
    }
    assert state == original
    # All data still exists in the source for exact Planner/policy validation.
    assert len(state['tool_capabilities']) == 7
    actual = read_snapshot(build(user_turn('current'), state=projected))
    assert actual['tool_capabilities'][0]['available'] is False
    assert actual['tool_capabilities'][2]['available'] is False
    assert actual['vehicle_skill_capabilities'] == projected['vehicle_skill_capabilities']


def test_absent_capability_rows_never_become_available_from_catalog_or_feature_flags():
    state = snapshot()
    state['tool_capabilities'] = []
    state['capabilities']['fold_mirror'] = True
    projected = project_talker_snapshot(state, {'set_volume'}, {'fold_mirror': ('mirrors', 'control')})
    assert projected['vehicle_skill_capabilities']['mirrors'] == {
        'available_count': 0, 'missing': ['fold_mirror']}
