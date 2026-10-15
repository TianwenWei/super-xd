"""Literal rejection tests; no model, routing, vehicle writes or network."""
from copy import deepcopy

import pytest

from backend.authorization import (
    validate_literal_authorization, validate_literal_feature_target, validate_literal_window_hold,
)


def guard(text, enabled, current=True, name='set_ambient_light'):
    return validate_literal_authorization(text, name, {'enabled': enabled}, {'lights': {'ambient': current}})


@pytest.mark.parametrize('text', [
    '先不要打开氛围灯', '氛围灯先别打开', '氛围灯开关别动', '氛围灯电源保持原状',
    '不要开启氛围灯，颜色调成蓝色，亮度设为40',
    '先别把氛围灯打开', '保持氛围灯电源原状', '不要动氛围灯的开关',
])
@pytest.mark.parametrize('enabled,current', [(False, True), (True, False), (False, False), (True, True)])
def test_literal_power_holds_refuse_both_booleans_even_same_value_with_exact_spans(text, enabled, current):
    record = guard(text, enabled, current)
    assert record['kind'] == 'literal_keep_state'
    assert record['executed'] is False and record['needs_clarification'] is False
    assert record['current_value'] is current and record['requested_value'] is enabled
    assert record['state_path'] == 'lights.ambient'
    assert record['evidence'] and all(item['meaning'] == 'keep_current_power' for item in record['evidence'])
    assert all(text[item['start']:item['end']] == item['text'] for item in record['evidence'])


@pytest.mark.parametrize('name,args', [
    ('set_ambient_color', {'color': 'blue'}),
    ('set_ambient_brightness', {'brightness': 40}),
    ('set_climate_power', {'enabled': False}),
])
def test_power_hold_does_not_interfere_with_color_brightness_or_other_functions(name, args):
    assert validate_literal_authorization('氛围灯电源保持原状，颜色蓝色，亮度40', name, args,
                                          {'lights': {'ambient': True}}) is None


@pytest.mark.parametrize('text,enabled', [
    ('不是关闭氛围灯，是打开氛围灯', True),
    ('不是要关闭氛围灯，是打开氛围灯', True),
    ('不是打开氛围灯，是关闭氛围灯', False),
    ('关闭氛围灯', False), ('氛围灯保持关闭', False), ('保持关闭', False),
    ('氛围灯不要亮了', False), ('不要亮了', False),
    ('打开氛围灯', True), ('氛围灯保持开启', True),
    ('不是保持氛围灯电源原状，是打开氛围灯', True),
])
def test_explicit_settings_and_corrective_sentences_are_not_mistaken_for_holds(text, enabled):
    assert guard(text, enabled) is None


@pytest.mark.parametrize('text', [
    '氛围灯开关别动，但打开氛围灯',
    '氛围灯电源保持原状，同时关闭氛围灯',
    '打开氛围灯，关闭氛围灯',
])
def test_literal_conflicting_new_power_setting_requires_clarification(text):
    record = guard(text, False)
    assert record['kind'] == 'literal_authorization_conflict'
    assert record['executed'] is False and record['needs_clarification'] is True
    assert len(record['evidence']) >= 2
    assert all(text[item['start']:item['end']] == item['text'] for item in record['evidence'])
    assert 'arguments' not in record and 'tool_name' not in record


@pytest.mark.parametrize('text,enabled', [('关闭氛围灯', True), ('打开氛围灯', False),
                                         ('氛围灯保持关闭', True), ('氛围灯不要亮了', True)])
def test_literal_target_opposing_selected_boolean_is_rejected_without_rewriting_it(text, enabled):
    record = guard(text, enabled)
    assert record['kind'] == 'literal_authorization_conflict' and record['needs_clarification']
    assert record['requested_value'] is enabled and record['executed'] is False


def test_source_arguments_and_snapshot_are_never_mutated_or_inferred_from_text():
    text = '现在氛围灯是关的，但电源开关别动；氛围灯电源保持原状。'
    arguments, snapshot = {'enabled': False}, {'lights': {'ambient': True, 'ambient_color': 'blue'}}
    before_args, before_state = deepcopy(arguments), deepcopy(snapshot)
    record = validate_literal_authorization(text, 'set_ambient_light', arguments, snapshot)
    assert record['current_value'] is True  # The user's description is not state authority.
    assert arguments == before_args and snapshot == before_state
    missing = validate_literal_authorization('氛围灯开关别动', 'set_ambient_light', arguments, {})
    assert missing['current_value_known'] is False and missing['current_value'] is None


@pytest.mark.parametrize('text', ['你好', '氛围灯颜色保持原状', '阅读灯先不要打开，氛围灯调蓝色'])
def test_uncovered_text_is_not_routed_or_treated_as_blanket_authorization(text):
    assert guard(text, False) is None


FEATURE_CASES = [
    ('后视镜加热', 'set_mirror_heating', {'enabled': False}),
    ('方向盘加热', 'set_steering_heating', {'enabled': False}),
    ('座椅加热', 'set_seat_heating', {'seat': 'driver', 'level': 0}),
    ('座椅通风', 'set_seat_ventilation', {'seat': 'driver', 'level': 0}),
    ('座椅按摩', 'set_seat_massage', {'seat': 'driver', 'mode': 'off'}),
]


@pytest.mark.parametrize('requested,selected', [
    (requested, selected) for requested in FEATURE_CASES for selected in FEATURE_CASES
    if requested[1] != selected[1]
])
def test_literal_feature_confusion_is_rejected_with_exact_target_span_and_retry(requested, selected):
    text = f'请关闭{requested[0]}'
    record = validate_literal_feature_target(text, selected[1], selected[2], {})
    assert record['kind'] == 'literal_target_mismatch'
    assert record['executed'] is False and record['retry_model'] is True
    assert record['needs_clarification'] is False
    assert len(record['evidence']) == 1
    item = record['evidence'][0]
    assert item['text'] == requested[0] and text[item['start']:item['end']] == item['text']
    assert 'arguments' not in record and 'tool_name' not in record and 'suggested_tool' not in record


@pytest.mark.parametrize('feature,name,args', FEATURE_CASES)
def test_matching_feature_and_multiple_explicit_features_are_not_rejected(feature, name, args):
    assert validate_literal_feature_target(f'关闭{feature}', name, args, {}) is None
    text = '关闭后视镜加热和方向盘加热，关闭座椅加热和座椅通风，再关闭座椅按摩'
    assert validate_literal_feature_target(text, name, args, {}) is None


@pytest.mark.parametrize('text', [
    '不是关闭后视镜加热，是关闭方向盘加热',
    '不是后视镜加热，是方向盘加热',
    '不是后视镜加热而是方向盘加热',
    '关闭方向盘加热，不是后视镜加热',
    '关闭后视镜加热，不，关闭方向盘加热',
    '不要打开后视镜加热，打开方向盘加热',
    '后视镜加热先不要打开，打开方向盘加热',
])
def test_correction_or_negated_alternative_does_not_authorize_the_rejected_feature(text):
    assert validate_literal_feature_target(text, 'set_steering_heating', {'enabled': False}, {}) is None
    record = validate_literal_feature_target(text, 'set_mirror_heating', {'enabled': False}, {})
    assert record['kind'] == 'literal_target_mismatch'
    assert all(item['text'] == '方向盘加热' for item in record['evidence'])
    assert all(text[item['start']:item['end']] == item['text'] for item in record['evidence'])


@pytest.mark.parametrize('text', [
    '把那个关掉', '关掉加热', '后视镜加热开着吗？',
    '后视镜加热现在是开的', '不要打开后视镜加热',
    '不是后视镜加热',
])
def test_feature_guard_requires_covered_affirmative_literal_evidence(text):
    assert validate_literal_feature_target(text, 'set_steering_heating', {'enabled': False}, {}) is None


def test_feature_guard_does_not_affect_unrelated_tools_or_rewrite_arguments_or_snapshot():
    text = '关闭后视镜的加热'
    args = {'enabled': False}
    snapshot = {'mirrors': {'heating': True}, 'seats': {'steering_heating': True}}
    before_args, before_state = deepcopy(args), deepcopy(snapshot)
    record = validate_literal_feature_target(text, 'set_steering_heating', args, snapshot)
    assert record['evidence'][0]['text'] == '后视镜的加热'
    assert args == before_args and snapshot == before_state
    assert validate_literal_feature_target(text, 'get_mirror_state', {'mirror': 'left'}, snapshot) is None
    assert validate_literal_feature_target(text, 'set_temperature', {'temperature': 23}, snapshot) is None


@pytest.mark.parametrize('text,name,args,span', [
    ('打开氛围灯和左后儿童锁', 'set_interior_light', {'zone': 'all', 'enabled': True}, '氛围灯'),
    ('开启氛围灯电源', 'set_interior_light', {'zone': 'front', 'enabled': True}, '氛围灯电源'),
    ('关闭车内照明灯', 'set_ambient_light', {'enabled': False}, '车内照明灯'),
    ('打开车内灯', 'set_ambient_light', {'enabled': True}, '车内灯'),
    ('不是打开车内照明灯，是打开氛围灯', 'set_interior_light', {'zone': 'all', 'enabled': True}, '氛围灯'),
    ('打开车内照明灯，氛围灯颜色调成蓝色', 'set_ambient_light', {'enabled': True}, '车内照明灯'),
])
def test_lighting_power_and_interior_light_confusion_is_rejected(text, name, args, span):
    record = validate_literal_feature_target(text, name, args, {})
    assert record['kind'] == 'literal_target_mismatch' and record['retry_model'] is True
    assert record['executed'] is False
    assert [item['text'] for item in record['evidence']] == [span]
    assert all(text[item['start']:item['end']] == item['text'] for item in record['evidence'])


@pytest.mark.parametrize('name,args', [
    ('set_ambient_light', {'enabled': True}),
    ('set_interior_light', {'zone': 'all', 'enabled': True}),
])
def test_explicit_both_lighting_features_are_allowed(name, args):
    assert validate_literal_feature_target('打开氛围灯和车内照明灯', name, args, {}) is None


@pytest.mark.parametrize('text,name,args', [
    ('打开阅读灯', 'set_interior_light', {'zone': 'all', 'enabled': True}),
    ('打开车内阅读灯', 'set_ambient_light', {'enabled': True}),
    ('氛围灯亮度调成60', 'set_interior_light', {'zone': 'all', 'enabled': True}),
    ('打开氛围灯', 'set_ambient_brightness', {'brightness': 60}),
    ('打开车内灯', 'set_ambient_color', {'color': 'blue'}),
    ('关闭后视镜加热', 'set_interior_light', {'zone': 'all', 'enabled': False}),
    ('打开氛围灯', 'set_steering_heating', {'enabled': True}),
])
def test_lighting_family_does_not_expand_into_other_functions(text, name, args):
    assert validate_literal_feature_target(text, name, args, {}) is None


@pytest.mark.parametrize('text,window', [
    ('副驾别动，其余三窗关', 'front_passenger'),
    ('副驾驶车窗保持不变，其余三窗关闭', 'front_passenger'),
    ('右前窗保持原状，其他车窗关上', 'front_passenger'),
    ('保留前右窗开度，关闭其他三窗', 'front_passenger'),
    ('主驾窗别动，其他窗关', 'driver'),
    ('别动左前车窗，其余车窗关闭', 'driver'),
    ('左后窗开度保持不变，关闭其他车窗', 'rear_left'),
    ('保留后排左侧车窗当前开度，其余窗关', 'rear_left'),
    ('右后窗不要动，其他窗关闭', 'rear_right'),
    ('保持后排右侧窗不变，其他车窗关上', 'rear_right'),
])
@pytest.mark.parametrize('current', [0, 37])
def test_all_window_write_cannot_override_literal_preserved_window_even_same_value(text, window, current):
    snapshot = {'windows': {'positions': {window: current}}}
    record = validate_literal_window_hold(text, 'close_all_windows', {}, snapshot)
    assert record['kind'] == 'literal_window_hold' and record['executed'] is False
    assert record['retry_model'] is True and record['needs_clarification'] is False
    assert record['preserved_windows'] == [window] and record['affected_windows'] == [window]
    assert record['current_positions'] == {window: current} and record['unknown_windows'] == []
    assert record['evidence']
    assert all(text[item['start']:item['end']] == item['text'] for item in record['evidence'])


@pytest.mark.parametrize('name,args', [
    ('set_window_position', {'window': 'front_passenger', 'position': 37}),
    ('set_window_position', {'window': 'front_passenger', 'position': 0}),
    ('adjust_window_position', {'window': 'front_passenger', 'delta': 0}),
    ('open_all_windows', {}),
])
def test_position_writers_intersecting_preserved_set_are_rejected_without_rewriting(name, args):
    text = '副驾车窗开度目前37，副驾别动，其余三窗关闭'
    snapshot = {'windows': {'positions': {'front_passenger': 61}}}
    original_args, original_state = deepcopy(args), deepcopy(snapshot)
    record = validate_literal_window_hold(text, name, args, snapshot)
    assert record['current_positions'] == {'front_passenger': 61}  # Only the snapshot is authority.
    assert record['executed'] is False and record['retry_model'] is True
    assert args == original_args and snapshot == original_state
    assert 'arguments' not in record and 'suggested_tool' not in record


@pytest.mark.parametrize('window', ['driver', 'rear_left', 'rear_right'])
def test_other_three_window_writes_are_permitted_by_hold_guard(window):
    assert validate_literal_window_hold('副驾别动，其余三窗关', 'set_window_position',
                                        {'window': window, 'position': 0}, {}) is None


@pytest.mark.parametrize('text', [
    '主驾窗和副驾窗保持不变，其余两窗关闭',
    '别动主驾窗和副驾窗，其余两窗关闭',
    '保留主驾和副驾开度，其余两车窗关闭',
])
def test_multiple_preserved_windows_and_missing_snapshot_values_are_explicit(text):
    record = validate_literal_window_hold(text, 'close_all_windows', {},
                                           {'windows': {'positions': {'driver': 12}}})
    assert record['preserved_windows'] == ['driver', 'front_passenger']
    assert record['current_positions'] == {'driver': 12}
    assert record['unknown_windows'] == ['front_passenger']
    assert {item['window'] for item in record['evidence']} == {'driver', 'front_passenger'}


@pytest.mark.parametrize('text,name,args', [
    ('副驾别动，打开空调', 'close_all_windows', {}),
    ('副驾座椅保持不变，其余车窗关闭', 'close_all_windows', {}),
    ('副驾别动，关闭天窗', 'close_all_windows', {}),
    ('副驾车窗别动', 'set_window_child_lock', {'enabled': True}),
    ('副驾车窗别动', 'set_window_anti_pinch', {'window': 'front_passenger', 'enabled': True}),
    ('副驾车窗别动', 'set_sunroof_position', {'position': 0}),
    ('不是副驾车窗别动，是把副驾车窗关闭', 'close_all_windows', {}),
    ('副驾车窗别动，不，全部车窗关闭', 'close_all_windows', {}),
])
def test_window_hold_requires_literal_context_and_does_not_cover_other_properties(text, name, args):
    assert validate_literal_window_hold(text, name, args, {}) is None
