"""Pure prompt-copy compaction; no model/network calls or archive mutation."""
from copy import deepcopy
import json

import pytest

from super_xd.nlp.prompts.catalog import load_skill
from super_xd.nlp.prompts.model_context import compact_message, compact_tool_result


def task_result():
    return {'ok': True, 'feedback_kind': 'progress', 'detail': '当前明确限制。', 'task': {
        'id': 'task', 'goal': '巴黎明天温度，保留用户的地点与日期条件',
        'status': 'completed', 'revision': 3, 'summary': '完整summary。' * 100,
        'question': None, 'error': None, 'constraints': ['不改变日期', '只有周二下雨才带伞'],
        'context': {'vehicle': {'very_large': '原始车况' * 3000},
                    'recent_history': [{'role': 'user', 'content': '原始会话保留'}]},
        'result': {'summary': '完整报告。' * 3000, 'spoken_summary': '具体结论，必要条件不变。',
                   'sources': [{'url': 'https://official.example/', 'queried_at': 'actual-time'}]},
        'transcript': [{'role': 'user', 'content': '完整原始记录'}],
        'sources': [{'url': 'https://official.example/'}], 'created_at': 'original-time',
    }}


def test_task_compaction_preserves_all_conditions_and_current_answer_without_mutating_archive():
    original = task_result()
    archive = deepcopy(original)
    compact = compact_tool_result(original, 'manage_task')
    assert original == archive
    assert set(compact['task']) == {'id', 'goal', 'status', 'revision', 'summary', 'question', 'constraints', 'result'}
    for key in ('goal', 'constraints', 'summary', 'question'):
        assert compact['task'][key] == archive['task'][key]
    assert compact['task']['result'] == {'spoken_summary': '具体结论，必要条件不变。'}
    assert compact['detail'] == '当前明确限制。'
    compact['task']['constraints'].append('修改返回对象')
    assert original == archive


def test_explicit_detailed_read_keeps_entire_report_sources_and_summary():
    original = task_result()
    compact = compact_tool_result(original, 'manage_task', read_details=True)
    assert compact['task']['result'] == original['task']['result']
    assert compact['task']['summary'] == original['task']['summary']
    compact['task']['result']['sources'][0]['url'] = 'changed-copy'
    assert original['task']['result']['sources'][0]['url'] == 'https://official.example/'


@pytest.mark.parametrize('result', [
    {'summary': '后置任何条件和否定都不能丢。' * 1000},
    {'summary': 'legacy全文。' * 1000, 'spoken_summary': ''},
    {'value': {'temperature': 20, 'unverified': True}},
    '旧版纯文字报告。' * 1000,
    None,
])
def test_legacy_or_unstructured_results_are_retained_without_text_cutoffs(result):
    original = {'task': {'id': 'legacy', 'result': result}}
    compact = compact_tool_result(original)
    expected = {'summary': result['summary']} if isinstance(result, dict) and 'summary' in result else result
    assert compact['task']['result'] == expected


@pytest.mark.parametrize('tool_name', [None, 'load_skill'])
def test_loaded_skill_keeps_complete_manifest_and_names_without_duplicate_schemas(tool_name):
    original = load_skill('seats')
    archive = deepcopy(original)
    compact = compact_tool_result(original, tool_name)
    assert compact == {'ok': True, 'skill': original['skill']}
    assert 'set_seat_massage' in compact['skill']['tool_names']
    assert compact['skill']['description'] == original['skill']['description']
    compact['skill']['tool_names'].clear()
    assert original == archive and original['tools']


def test_unknown_payload_that_mentions_tools_is_not_mistaken_for_catalog_capabilities():
    body = {'ok': True, 'skill': {'id': 'untrusted-page'}, 'tools': [{'content': '真实页面事实'}]}
    assert compact_tool_result(body) == body
    body['skill']['tool_names'] = ['fictional']
    assert compact_tool_result(body, 'other_tool') == body


def test_vehicle_values_refusals_and_nested_state_remain_exact():
    body = {'ok': False, 'name': 'get_window_state', 'arguments': {'window': 'driver'},
            'value': {'position': 50, 'state': {'important_condition': '未满足就不执行'}},
            'reason': '车速超过80时不能开到一半以上。' * 100,
            'state': {'duplicate_current_full_vehicle': 'large'}}
    archive = deepcopy(body)
    compact = compact_tool_result(body, 'get_window_state')
    assert compact == {key: value for key, value in body.items() if key != 'state'}
    compact['value']['state']['important_condition'] = 'copy-only'
    assert body == archive


@pytest.mark.parametrize('record', [
    {'role': 'user', 'content': '{"state":"用户给出的事实，必须保留"}'},
    {'role': 'assistant', 'content': '用户明确要求的完整说明。' * 1000,
     'tool_calls': [{'id': 'call', 'function': {'name': 'set_seat_massage', 'arguments': '{"seat":"driver","mode":"off"}'}}]},
    {'role': 'tool', 'tool_call_id': 'id', 'content': '非JSON工具原文不要丢。'},
    {'role': 'tool', 'content': None},
    None,
])
def test_only_valid_json_tool_messages_change(record):
    archive = deepcopy(record)
    compact = compact_message(record)
    assert compact == archive and record == archive
    if isinstance(compact, dict):
        compact['new_field'] = 'independent'
        assert record == archive


def test_message_preserves_protocol_id_and_independent_archive_while_compacting_copy():
    body = task_result()
    record = {'role': 'tool', 'tool_call_id': 'real-call-id', 'content': json.dumps(body, ensure_ascii=False)}
    archive = deepcopy(record)
    compact = compact_message(record, 'manage_task')
    assert compact['tool_call_id'] == record['tool_call_id'] and compact['role'] == 'tool'
    assert record == archive
    assert json.loads(compact['content'])['task']['constraints'] == body['task']['constraints']
    assert len(compact['content']) < len(record['content']) / 3
    detailed = compact_message(record, 'manage_task', read_details=True)
    assert json.loads(detailed['content'])['task']['result'] == body['task']['result']
    assert record == archive


def test_batch_task_compaction_keeps_independent_conditions_answers_and_entire_detail():
    first, second = task_result(), task_result()
    second['task']['id'] = 'second'
    second['task']['goal'] = '另一目标，不能混用第一项条件'
    second['task']['constraints'] = ['只核实第二项事实', '未确认时不要执行']
    second['task']['result']['spoken_summary'] = '第二项仍不能确认。'
    body = {'ok': True, 'tasks': [first['task'], second['task']], 'items': [first, second],
            'feedback_kind': 'progress', 'detail': '完整各目标和实际进度，不能裁剪。' * 300}
    archive = deepcopy(body)
    compact = compact_tool_result(body, 'manage_task')
    assert body == archive and compact['detail'] == body['detail']
    assert compact['tasks'][0] == compact['items'][0]['task']
    assert compact['tasks'][1] == compact['items'][1]['task']
    assert compact['tasks'][0]['constraints'] != compact['tasks'][1]['constraints']
    assert compact['tasks'][0]['result']['spoken_summary'] == '具体结论，必要条件不变。'
    assert compact['tasks'][1]['result']['spoken_summary'] == '第二项仍不能确认。'
    compact['items'][1]['task']['constraints'].append('only-copy')
    assert 'only-copy' not in compact['tasks'][1]['constraints'] and body == archive
    detailed = compact_tool_result(body, 'manage_task', read_details=True)
    assert [task['result'] for task in detailed['tasks']] == [task['result'] for task in body['tasks']]
    assert [item['task']['result'] for item in detailed['items']] == [task['result'] for task in body['tasks']]
