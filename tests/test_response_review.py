"""The evaluator uses a configured role and rejects unsupported judge claims."""
from copy import deepcopy
import json

import pytest

from evals.response_review import review_materials, review_results


class Judge:
    def __init__(self, change=None, fail=False):
        self.roles, self.requests = [], []
        self.change, self.fail = change, fail

    def for_role(self, role):
        self.roles.append(role)
        return self

    async def complete(self, messages, tools, **options):
        self.requests.append((messages, tools, options))
        if self.fail:
            raise RuntimeError('provider refused Authorization: Bearer sk-private-fixture-value')
        payload = json.loads(messages[1]['content'])
        items = [{'rubric_index': index, 'passed': True, 'reason': '回答明确限定资料范围',
                  'evidence': [{'source_id': 'current_response', 'quote': '全天预报'}]}
                 for index in range(len(payload['rubrics']))]
        if self.change:
            self.change(items)
        return {'role': 'assistant', 'tool_calls': [{'id': 'review', 'type': 'function', 'function': {
            'name': 'submit_response_review', 'arguments': json.dumps({'items': items})}}],
            '_meta': {'model': 'configured-judge', 'provider': 'fixture', 'usage': {'prompt_tokens': 100}}}


def cases():
    return [{'id': 'generic', 'passed': True, 'turns': [
        {'input': '上一个问题', 'text': '上一条结果', 'playback': {'status': 'partial', 'heard_text': '上一条'}},
        {'input': '指定时段的情况', 'text': '只有全天预报，不能确认这个时刻。', 'passed': True,
         'lookup_results': [{'tool': 'get_weather', 'result': {'summary': {'min_c': 12, 'max_c': 21}}}],
         'response_rubric': ['保留时间粒度', '不能将每日最大值当成当前温度']},
    ]}]


async def test_configured_thinker_reviews_each_rubric_with_real_material_quotes():
    results, judge = cases(), Judge()
    summary = await review_results(judge, results, concurrency=2)
    assert judge.roles == ['thinker']
    assert len(judge.requests) == 1
    review = results[0]['turns'][1]['response_review']
    assert review['status'] == 'reviewed' and review['passed'] is True
    assert review['model_metadata']['model'] == 'configured-judge'
    assert summary == {'requested': 1, 'reviewed': 1, 'passed': 1, 'failed': 0, 'unreviewed': 0,
                       'human_spot_check_required': True}
    payload = json.loads(judge.requests[0][0][1]['content'])
    assert {'history_0', 'receipt_0', 'current_response'} <= {item['id'] for item in payload['materials']}
    assert results[0]['passed'] is True  # Combining semantic/structural grades is the CLI's responsibility.


@pytest.mark.parametrize('change', [
    lambda items: items[0].update(evidence=[{'source_id': 'current_response', 'quote': '材料中不存在的结论'}]),
    lambda items: items[1].update(rubric_index=0),
    lambda items: items[0].update(evidence=[{'source_id': 'invented', 'quote': '全天预报'}]),
    lambda items: items[0].update(passed='true'),
])
async def test_invalid_structured_verdict_or_fabricated_quote_is_unreviewed(change):
    results = cases()
    summary = await review_results(Judge(change), results)
    review = results[0]['turns'][1]['response_review']
    assert review['status'] == 'unreviewed' and review['passed'] is None
    assert summary['unreviewed'] == 1 and summary['passed'] == 0


async def test_provider_failure_is_not_a_pass_or_a_leaked_diagnostic():
    results = cases()
    summary = await review_results(Judge(fail=True), results)
    assert summary['unreviewed'] == 1
    assert 'sk-private-fixture-value' not in json.dumps(results)
    assert 'provider refused' in results[0]['turns'][1]['response_review']['error_message']
    assert results[0]['turns'][1]['response_review']['raw_response'] is None
    assert results[0]['turns'][1]['response_review']['passed'] is None


async def test_failed_quote_keeps_exact_diagnostic_and_redacted_raw_response():
    class RawJudge(Judge):
        async def complete(self, *args, **kwargs):
            response = await super().complete(*args, **kwargs)
            response['_meta']['raw_response'] = {'api_key': 'secret-value', 'status': 'ok'}
            return response

    results = cases()
    await review_results(RawJudge(lambda items: items[0].update(evidence=[{
        'source_id': 'current_response', 'quote': '未出现的说法'}])), results)
    review = results[0]['turns'][1]['response_review']
    assert review['status'] == 'unreviewed'
    assert review['error_details'] == {'rubric_index': 0, 'evidence_index': 0,
                                       'source_id': 'current_response', 'quote': '未出现的说法'}
    assert 'quote does not exist' in review['error_message']
    assert review['raw_response']['_meta']['raw_response'] == {'api_key': '[redacted]', 'status': 'ok'}
    assert 'secret-value' not in json.dumps(review)


@pytest.mark.parametrize(('quote', 'reviewed'), [
    ('"name": "滨河停车场", "address": "南京滨河路"', True),
    ('"address": "南京滨河路", "name": "滨河停车场"', False),
    ('"name": "滨河停车场", "address": "上海滨河路"', False),
])
async def test_cross_field_quotes_must_match_the_presented_json_order_and_values(quote, reviewed):
    results = cases()
    results[0]['turns'][1]['lookup_results'] = [{'tool': 'search_places', 'result': {
        'places': [{'name': '滨河停车场', 'address': '南京滨河路'}]}}]
    summary = await review_results(Judge(lambda items: items[0].update(evidence=[{
        'source_id': 'receipt_0', 'quote': quote}])), results)
    assert summary['reviewed'] == int(reviewed)
    assert summary['unreviewed'] == int(not reviewed)


async def test_complete_action_object_can_be_quoted_without_reordering_nested_fields():
    results = cases()
    command = {'name': 'set_seat_ventilation', 'arguments': {'seat': 'driver', 'level': 1}, 'source': 'talker'}
    results[0]['turns'][1]['commands'] = [command]
    summary = await review_results(Judge(lambda items: items[0].update(evidence=[{
        'source_id': 'commands', 'quote': json.dumps(command, ensure_ascii=False)}])), results)
    assert summary['reviewed'] == summary['passed'] == 1


@pytest.mark.parametrize('preface_count', [1, 2])
async def test_preface_is_a_timing_view_of_full_speech_without_deduplicating_real_repetitions(preface_count):
    results, judge = cases(), Judge()
    turn = results[0]['turns'][1]
    turn['preface'] = '我先核对资料。'
    turn['text'] = (turn['preface'] + '\n') * preface_count + '目前只有全天预报。'
    await review_results(judge, results)
    messages = judge.requests[0][0]
    materials = {item['id']: item for item in json.loads(messages[1]['content'])['materials']}
    assert materials['current_response']['value'] == turn['text']
    assert materials['current_response']['value'].count(turn['preface']) == preface_count
    assert materials['current_preface'] == {
        'id': 'current_preface', 'kind': 'assistant_preface', 'value': turn['preface'],
        'view_of': 'current_response', 'timing': 'before_tool_dispatch', 'separate_speech_event': False}
    assert '已经包含current_preface' in messages[0]['content']
    assert '不能把两个材料视图相同当成两次播报' in messages[0]['content']
    assert '不能合并完整流内真实重复的内容' in messages[0]['content']


def test_proposed_queries_cannot_replace_the_recorded_execution_arguments():
    turn = {'input': '查某机构的服务地区', 'text': '资料不足',
            'talker_calls': [{'name': 'web_search', 'arguments': {'query': '模型提议的主体'}}],
            'lookup_results': [{'tool': 'web_search', 'arguments': {'query': '实际请求的主体'},
                                'result': {'ok': False, 'error': 'source_unavailable'}}]}
    original = deepcopy(turn)
    materials = {item['id']: item for item in review_materials(turn, [])}
    assert materials['proposed_tool_calls']['kind'] == 'model_tool_proposals'
    executed = materials['executed_lookups']['value']
    assert executed == {'records': turn['lookup_results'], 'arguments_recorded': [True]}
    assert materials['receipt_0']['value'] == executed['records'][0]
    assert materials['receipt_0']['view_of'] == 'executed_lookups'
    assert materials['receipt_0']['record_index'] == 0
    assert turn == original


def test_rejected_proposals_and_missing_legacy_arguments_are_not_invented_as_executions():
    proposal = {'name': 'web_search', 'arguments': {'query': '未执行的提议'}}
    turn = {'text': '尚未执行', 'talker_calls': [proposal], 'lookup_results': []}
    materials = {item['id']: item for item in review_materials(turn, [])}
    assert materials['executed_lookups']['value'] == {'records': [], 'arguments_recorded': []}
    assert 'receipt_0' not in materials
    turn['lookup_results'] = [{'tool': 'web_search', 'result': {'ok': True, 'results': []}}]
    materials = {item['id']: item for item in review_materials(turn, [])}
    executed = materials['executed_lookups']['value']
    assert executed['arguments_recorded'] == [False]
    assert 'arguments' not in executed['records'][0]
    assert materials['proposed_tool_calls']['value'] == [proposal]


async def test_query_material_semantics_are_sent_to_judge_without_adding_rubrics():
    results, judge = cases(), Judge()
    original_rubrics = deepcopy(results[0]['turns'][1]['response_rubric'])
    await review_results(judge, results)
    messages = judge.requests[0][0]
    payload = json.loads(messages[1]['content'])
    assert payload['rubrics'] == original_rubrics
    assert '被拒绝的提议不能当成已执行' in messages[0]['content']
    assert '不能从提议反推执行参数' in messages[0]['content']
    assert '不增加评分标准' in messages[0]['content']


async def test_semantic_failure_is_distinct_from_missing_review():
    results = cases()
    summary = await review_results(Judge(lambda items: items[0].update(passed=False)), results)
    assert summary['failed'] == 1 and summary['unreviewed'] == 0


async def test_no_rubric_does_not_invoke_judge_and_input_data_is_not_rewritten():
    results = [{'turns': [{'input': '问候', 'text': '你好'}]}]
    original, judge = deepcopy(results), Judge()
    summary = await review_results(judge, results)
    assert summary['requested'] == 0 and not judge.requests and results == original


@pytest.mark.parametrize('invalid', [0, -1, True, 1.5])
async def test_nonpositive_or_noninteger_concurrency_is_rejected(invalid):
    with pytest.raises(ValueError):
        await review_results(Judge(), cases(), concurrency=invalid)


async def test_review_summary_ignores_saved_verdicts_for_turns_without_rubrics():
    results = cases()
    results[0]['turns'][0]['response_review'] = {'status': 'reviewed', 'passed': False, 'items': []}
    summary = await review_results(Judge(), results)
    assert summary['requested'] == summary['reviewed'] == summary['passed'] == 1
    assert summary['failed'] == summary['unreviewed'] == 0
