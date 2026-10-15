from backend.speech_text import speech_text, brief_result


def test_markup_guard_preserves_condition_negation_number_and_real_answer():
    spoken = speech_text('# 结论\n- 车还在行驶，**暂时不能打开尾门**。停稳后再试。\n- 车外温度3.5℃，电量50%。\n来源：https://example.com/check [1]')
    assert spoken.startswith('车还在行驶，暂时不能打开尾门。停稳后再试。')
    assert '3.5度' in spoken and '百分之50' in spoken
    assert not any(marker in spoken for marker in ('#', '*', 'https', '[1]'))


def test_tables_and_json_keep_their_information_in_oral_form():
    table = speech_text('| 城市 | 天气 |\n|---|---|\n| 巴黎 | 雨天 |')
    assert '巴黎' in table and '雨天' in table and '|' not in table
    structured = speech_text('结果：{"summary":"今天有雨，暂时不能确认停车位。","temperature":23}')
    assert '今天有雨' in structured and '不能确认停车位' in structured and '23' in structured
    assert not any(marker in structured for marker in ('{', '}', '"'))


def test_complete_text_guard_handles_markup_that_crossed_model_delta_boundaries():
    deltas = ['**车还', '在行驶**，暂时不能', '打开尾门。[官网](https://exa', 'mple.com/help)']
    spoken = speech_text(''.join(deltas))
    assert '车还在行驶，暂时不能打开尾门。' in spoken
    assert '官网' in spoken and 'https' not in spoken and '*' not in spoken


def test_legacy_report_guard_retains_all_facts_despite_old_length_arguments():
    original = '# 结论\n优先考虑市中心的室内场馆。\n距离较近。\n可以比较票价。\n营业时间尚未核实，出发前需要确认。\n这是完整报告的其他详细证据。'
    spoken = brief_result(original, max_chars=20, max_sentences=1)
    assert spoken.startswith('优先考虑市中心的室内场馆。')
    assert '营业时间尚未核实，出发前需要确认。' in spoken
    assert spoken == speech_text(original)
    assert spoken.endswith('这是完整报告的其他详细证据。')


def test_no_hard_cut_splits_one_long_condition_or_decimal():
    condition = '只有车辆停稳并且确认后方没有障碍物时才可以打开尾门，否则暂时不要操作。'
    assert brief_result(condition, max_chars=10, max_sentences=1) == condition
    assert speech_text('3.5度，仍然有点冷。').startswith('3.5度')


def test_legacy_short_report_keeps_later_booking_condition_and_alternative():
    original = '推荐去A馆。位置近。票价低。需要先预约成功才能入场。如果预约失败，就改去B馆。'
    spoken = brief_result(original)
    assert '需要先预约成功才能入场。' in spoken
    assert '如果预约失败，就改去B馆。' in spoken
    assert spoken == original


def test_reference_words_in_actual_statements_are_not_source_labels():
    original = '沿途风景可供参考。路线只作为参考。这项建议仅供参考。'
    assert speech_text(original) == original


def test_full_width_letters_become_ascii_but_cjk_punctuation_stays():
    assert speech_text('好，导航去ｉ１７ ＣＡＦＥ？') == '好，导航去i17 CAFE？'
