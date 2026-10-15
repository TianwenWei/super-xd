"""Acceptance math must retain failed requests; these records are explicit fixtures."""
from copy import deepcopy
import pytest

from scripts.audio_benchmark import aggregate, plan


def record(request='a', duration=1000):
    return {'client_request_id': request, 'query': '你好，准备出发啦。', 'turn_id': f'turn-{request}',
            'playback_id': request * 32, 'submit_perf_ms': 100,
            'first_pcm_perf_ms': 100 + duration, 'first_nonzero_pcm_perf_ms': 100 + duration,
            'first_play_scheduled_perf_ms': 101 + duration,
            'first_render_observed_perf_ms': 110 + duration,
            'first_nonzero_render_observed_perf_ms': 120 + duration,
            'pcm_bytes': 48, 'pcm_packets': 2, 'status': 'ended', 'context_state': 'running',
            'vehicle_receipts': [], 'task_ids': [], 'background_tasks': []}


def test_one_slow_attempt_fails_every_request_target_even_when_median_passes():
    result = aggregate([record('a', 800), record('b', 1500), record('c', 2400)])
    summary = result['summary']
    assert summary['attempts'] == 3 and summary['accepted_within_target'] == 2
    assert summary['within_target_ratio_all_attempts'] == .666667
    assert summary['p50_ms_observed_only'] == 1500 and summary['p95_ms_observed_only'] == 2310
    assert not summary['all_requests_target_met']
    assert result['attempts'][2]['failure_reasons'] == ['first_nonzero_pcm_exceeds_target']
    assert result['playback_observation']['first_nonzero_render_observed_ms']['p50_ms_observed_only'] == 1520


def test_current_1500ms_boundary_and_explicit_historical_target_keep_raw_clocks():
    records = [record('a', 1500), record('b', 1500.1), record('c', 2000), record('d', 2000.1)]
    original = deepcopy(records)
    current = aggregate(records)
    historical = aggregate(records, target_ms=2000)
    assert records == original
    assert current['target_ms'] == 1500 and current['summary']['accepted_within_target'] == 1
    assert historical['target_ms'] == 2000 and historical['summary']['accepted_within_target'] == 3
    assert current['summary']['received_nonzero_pcm_within_target'] == 1
    assert current['summary']['within_target_ratio_all_attempts'] == .25
    assert current['groups']['scenario_id']['greeting']['accepted_within_target'] == 1
    assert historical['groups']['scenario_id']['greeting']['accepted_within_target'] == 3
    assert [item['timings'] for item in current['attempts']] == [item['timings'] for item in historical['attempts']]
    assert plan()['target_ms'] == 1500 and plan(target_ms=2000)['target_ms'] == 2000


@pytest.mark.parametrize('target', [0, -1, float('nan'), float('inf'), True])
def test_invalid_target_cannot_produce_an_acceptance_report(target):
    with pytest.raises(ValueError, match='positive finite'):
        aggregate([record()], target_ms=target)


def test_cancelled_missing_audio_and_malformed_submissions_remain_denominator():
    missing = record('b'); missing.pop('first_nonzero_pcm_perf_ms'); missing['status'] = 'cancelled'
    result = aggregate([record(), missing, 'invalid record'])
    assert result['summary']['attempts'] == 3 and result['summary']['accepted_within_target'] == 1
    assert result['summary']['within_target_ratio_all_attempts'] == .333333
    assert 'missing_real_nonzero_pcm' in result['attempts'][1]['failure_reasons']
    assert result['attempts'][2]['raw']['raw_invalid_record'] == 'invalid record'


def test_silent_first_pcm_cannot_substitute_for_later_nonzero_pcm():
    item = record(duration=2300)
    item['first_pcm_perf_ms'] = 200
    item['diagnostics'] = {'provider_first_packet_ms': 20, 'server_first_audio_ms': 30}
    result = aggregate([item])
    assert result['attempts'][0]['timings']['first_pcm_ms'] == 100
    assert result['attempts'][0]['timings']['first_nonzero_pcm_ms'] == 2300
    assert not result['summary']['all_requests_target_met']


def test_vehicle_requires_actual_same_turn_state_and_receipt_before_audio():
    item = record()
    item['query'] = '把左前车窗打开到50%'
    item['vehicle_receipts'] = [{'turn_id': item['turn_id'], 'received_perf_ms': 900,
                                 'result': {'ok': True, 'name': 'set_window_position',
                                            'state': {'windows': {'positions': {'driver': 50}}}}}]
    assert aggregate([item])['summary']['all_requests_target_met']
    bad = deepcopy(item)
    bad['vehicle_receipts'][0]['received_perf_ms'] = 1200
    bad['vehicle_receipts'][0]['turn_id'] = 'old-turn'
    bad['vehicle_receipts'][0]['result']['state']['windows']['positions']['driver'] = 0
    errors = aggregate([bad])['attempts'][0]['semantic_errors']
    assert set(errors) == {'vehicle_receipt_not_observed_before_audio', 'vehicle_receipt_wrong_turn',
                           'vehicle_target_state_not_verified'}


def test_loaded_chat_needs_observed_real_background_tasks_at_submission():
    item = record()
    item['query'] = '用一句话形容秋天。'
    labels = {'a': {'scenario_id': 'loaded_chat', 'sample_phase': 'warm'}}
    assert not aggregate([item], labels)['summary']['all_requests_target_met']
    item['background_tasks'] = [{'id': 'task-1', 'status': 'running'}, {'id': 'task-2', 'status': 'queued'}]
    result = aggregate([item], labels)
    assert result['summary']['all_requests_target_met']
    assert result['groups']['sample_phase']['warm']['attempts'] == 1


def test_proactive_audio_is_separate_but_unmatched_submitted_requests_are_not_dropped():
    proactive = {'turn_id': 'proactive', 'playback_id': 'p' * 32, 'status': 'ended'}
    unmatched = {'client_request_id': 'unmatched', 'submit_perf_ms': 100, 'status': 'pending'}
    result = aggregate({'records': [record(), proactive, unmatched], 'time_domain': 'browser performance.now'})
    assert result['summary']['attempts'] == 2
    assert result['proactive_audio_excluded_from_query_denominator'] == [proactive]
    assert result['source_metadata']['time_domain'] == 'browser performance.now'


def test_duplicate_correlation_and_reversed_clocks_are_failures_not_cleaned_up():
    item = record(); bad = deepcopy(item); bad['first_nonzero_pcm_perf_ms'] = 50
    result = aggregate([item, bad])
    assert result['summary']['attempts'] == 2 and result['summary']['accepted_within_target'] == 0
    assert 'duplicate_request_id' in result['attempts'][0]['failure_reasons']
    assert 'invalid_clock_order:first_nonzero_pcm_perf_ms' in result['attempts'][1]['failure_reasons']
    assert plan()['planned_attempts'] == 14


def test_repeated_delegate_can_report_verified_existing_task_progress_without_rewriting_query():
    item = record(duration=3847)
    item['query'] = '请查询巴黎今天的天气，并核对真实来源。'
    item['background_tasks'] = [{'id': 'existing-task', 'status': 'running'}]
    labels = {'a': {'scenario_id': 'delegate', 'observed_route': 'task_progress'}}
    missing = aggregate([item], labels)
    assert missing['attempts'][0]['semantic_errors'] == ['existing_task_progress_not_verified_by_actual_tool_result']
    evidence = {'events': [{'turn_id': item['turn_id'], 'kind': 'tool.completed', 'status': 'completed',
                           'data': {'name': 'manage_task', 'arguments': '{"action":"get","task_id":"existing-task"}',
                                    'result': {'ok': True, 'task': {'id': 'existing-task', 'status': 'running'}}}}]}
    result = aggregate([item], labels, evidence)
    assert result['attempts'][0]['query'] == item['query']
    assert result['attempts'][0]['scenario_id'] == 'delegate'
    assert not result['attempts'][0]['semantic_errors']
    assert result['attempts'][0]['failure_reasons'] == ['first_nonzero_pcm_exceeds_target']
    assert result['summary']['attempts'] == 1 and not result['summary']['all_requests_target_met']
