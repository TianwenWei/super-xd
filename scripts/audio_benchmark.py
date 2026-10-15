"""Aggregate exported browser clocks. This command never sends model/TTS requests.

Input is the UI's audio measurement JSON (a list or {records: [...]}); optional
manifest metadata may label each client_request_id with scenario_id/sample_phase.
Missing observations and failed attempts remain in the acceptance denominator.
"""
import argparse
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


TARGET_MS = 1500.0
SCENARIOS = [
    {'scenario_id': 'greeting', 'query': '你好，准备出发啦。', 'semantic': 'chat'},
    {'scenario_id': 'chat', 'query': '今天终于完成一件拖了很久的小事，陪我聊一句吧。', 'semantic': 'chat'},
    {'scenario_id': 'temperature', 'query': '把空调温度设为23度', 'semantic': 'vehicle', 'tool': 'set_temperature'},
    {'scenario_id': 'window', 'query': '把左前车窗打开到50%', 'semantic': 'vehicle', 'tool': 'set_window_position'},
    {'scenario_id': 'navigation', 'query': '把模拟导航目的地设为上海虹桥站', 'semantic': 'vehicle', 'tool': 'set_navigation_destination'},
    {'scenario_id': 'delegate', 'query': '请查询巴黎今天的天气，并核对真实来源。', 'semantic': 'delegate'},
    {'scenario_id': 'loaded_volume', 'query': '把音量设为35%', 'semantic': 'vehicle', 'tool': 'set_volume', 'minimum_active_background_tasks': 2},
]
SCENARIO_BY_ID = {item['scenario_id']: item for item in SCENARIOS}
SCENARIO_BY_ID['loaded_chat'] = {'scenario_id': 'loaded_chat', 'query': '用一句话形容秋天。',
                                'semantic': 'chat', 'minimum_active_background_tasks': 2}
SCENARIO_BY_QUERY = {item['query'].rstrip('。'): item for item in SCENARIOS}
TIME_FIELDS = {
    'first_pcm_ms': 'first_pcm_perf_ms',
    'first_nonzero_pcm_ms': 'first_nonzero_pcm_perf_ms',
    'first_enqueue_ms': 'first_play_scheduled_perf_ms',
    'first_render_observed_ms': 'first_render_observed_perf_ms',
    'first_nonzero_render_observed_ms': 'first_nonzero_render_observed_perf_ms',
}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def percentile(values, fraction):
    """Linear interpolation, matching common NumPy/R type-7 percentiles."""
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lo, hi = math.floor(index), math.ceil(index)
    return round(values[lo] + (values[hi] - values[lo]) * (index - lo), 3)


def validate_target(target_ms):
    if not number(target_ms) or target_ms <= 0:
        raise ValueError('target_ms must be a positive finite number.')
    return float(target_ms)


def plan(repeats=2, target_ms=TARGET_MS):
    target_ms = validate_target(target_ms)
    return {
        'source': 'sampling_plan_only_no_requests_executed',
        'target_ms': target_ms,
        'target': f'browser_submit_to_first_real_nonzero_pcm_ms <= {target_ms:g} for every attempt',
        'planned_attempts': len(SCENARIOS) * repeats,
        'scenarios': [{**item, 'repeats': repeats} for item in SCENARIOS],
        'optional_scenario': {**SCENARIO_BY_ID['loaded_chat'], 'repeats': repeats},
        'instructions': [
            'Use the visible browser form and enable the real Speech AudioContext with a user gesture.',
            'Only the first actual query after the final service restart is first-query-after-restart.',
            'Warm samples reuse the page, session and running AudioContext. Wait for prior playback to drain.',
            'Later samples are warm/progressive-history; record connection reuse/prewarm diagnostics rather than claiming a cold provider.',
            'Loaded samples require at least two real queued/running Thinker tasks at submission.',
            f'Capture every submitted attempt, including failures, cancellation, missing audio and >{target_ms:g}ms.',
            'First nonzero PCM arrival uses the same-page performance.now clock as form submission.',
            'Report Worklet first nonzero render observation separately; it is not acoustic onset.',
            'Vehicle audio must follow the successful same-turn executor receipt; delegation must be registered.',
            'No static audio, placeholder speech, silent warm-up packets or provider-only clocks count.',
        ],
    }


def unpack(payload):
    if isinstance(payload, list):
        return payload, {}
    if not isinstance(payload, dict):
        raise ValueError('Browser export must be a JSON list or object with records.')
    records = payload.get('records', payload.get('measurements'))
    if not isinstance(records, list):
        raise ValueError('Browser export must include a records array.')
    return records, {k: v for k, v in payload.items() if k not in {'records', 'measurements'}}


def semantic_errors(record, scenario, first_audio, evidence=()):
    if not scenario:
        return ['unknown_scenario_semantics_not_verified']
    errors = []
    receipts = record.get('vehicle_receipts') or []
    task_ids = record.get('task_ids') or []
    if not isinstance(receipts, list) or not isinstance(task_ids, list):
        return ['invalid_semantic_evidence']
    if scenario['semantic'] == 'chat' and (receipts or task_ids):
        errors.append('unauthorized_vehicle_or_delegate_action')
    if scenario['semantic'] == 'delegate':
        registered = record.get('task_registered_perf_ms')
        if not task_ids:
            errors.append('missing_registered_task')
        if not number(registered) or not number(first_audio) or registered > first_audio:
            errors.append('task_registration_not_observed_before_audio')
        if receipts:
            errors.append('unexpected_vehicle_action')
    if scenario['semantic'] == 'task_progress':
        if receipts or task_ids:
            errors.append('unexpected_action_during_existing_task_progress')
        observed_tasks = {task.get('id') for task in record.get('background_tasks', []) if isinstance(task, dict)}
        verified = False
        for event in evidence:
            if (event.get('turn_id') != record.get('turn_id') or event.get('kind') != 'tool.completed'
                    or event.get('status') != 'completed'):
                continue
            data = event.get('data', {})
            args = data.get('arguments', {})
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except json.JSONDecodeError:
                continue
            result = data.get('result', {})
            returned = result.get('task', {}).get('id')
            if (data.get('name') == 'manage_task' and isinstance(args, dict) and args.get('action') == 'get'
                    and result.get('ok') is True and returned in observed_tasks
                    and args.get('task_id', returned) == returned):
                verified = True
        if not verified:
            errors.append('existing_task_progress_not_verified_by_actual_tool_result')
    if scenario['semantic'] == 'vehicle':
        if not receipts:
            errors.append('missing_vehicle_receipt')
        elif len(receipts) != 1:
            errors.append('unexpected_vehicle_receipt_count')
        if task_ids:
            errors.append('unexpected_delegate')
        for receipt in receipts:
            if not isinstance(receipt, dict):
                errors.append('invalid_vehicle_receipt')
                continue
            result = receipt.get('result', {})
            if not isinstance(result, dict) or result.get('ok') is not True:
                errors.append('vehicle_execution_failed')
            if isinstance(result, dict) and result.get('name') != scenario['tool']:
                errors.append('wrong_vehicle_tool')
            if receipt.get('turn_id') != record.get('turn_id'):
                errors.append('vehicle_receipt_wrong_turn')
            received = receipt.get('received_perf_ms')
            if not number(received) or not number(first_audio) or received > first_audio:
                errors.append('vehicle_receipt_not_observed_before_audio')
            # Receipt state validates the executed value, not just the ACK.
            state = result.get('state') if isinstance(result, dict) else None
            if scenario['scenario_id'] == 'temperature':
                valid = isinstance(state, dict) and state.get('climate', {}).get('temperatures', {}).get('driver') == 23
            elif scenario['scenario_id'] == 'window':
                valid = isinstance(state, dict) and state.get('windows', {}).get('positions', {}).get('driver') == 50
            elif scenario['scenario_id'] == 'loaded_volume':
                valid = isinstance(state, dict) and state.get('media', {}).get('volume') == 35
            elif scenario['scenario_id'] == 'navigation':
                valid = isinstance(state, dict) and state.get('navigation', {}).get('destination') == '上海虹桥站'
            else:
                valid = False
            if not valid:
                errors.append('vehicle_target_state_not_verified')
    required = scenario.get('minimum_active_background_tasks', 0)
    tasks = record.get('background_tasks') or []
    active = sum(isinstance(task, dict) and task.get('status') in {'queued', 'running'} for task in tasks)
    if active < required:
        errors.append('insufficient_real_background_load_at_submission')
    return sorted(set(errors))


def summarize_attempts(attempts, target_ms=TARGET_MS):
    total = len(attempts)
    observed = [item['timings']['first_nonzero_pcm_ms'] for item in attempts
                if item['timings']['first_nonzero_pcm_ms'] is not None and not item['measurement_errors']]
    success = [item for item in attempts if item['accepted']]
    timely = sum(item['timings']['first_nonzero_pcm_ms'] is not None
                 and item['timings']['first_nonzero_pcm_ms'] <= target_ms
                 and not item['measurement_errors'] for item in attempts)
    return {
        'attempts': total,
        'valid_first_nonzero_pcm_observations': len(observed),
        'target_ms': target_ms,
        'received_nonzero_pcm_within_target': timely,
        'received_within_target_ratio_all_attempts': round(timely / total, 6) if total else 0,
        'accepted_within_target': len(success),
        'within_target_ratio_all_attempts': round(len(success) / total, 6) if total else 0,
        'all_requests_target_met': bool(total) and len(success) == total,
        'p50_ms_observed_only': percentile(observed, .5),
        'p95_ms_observed_only': percentile(observed, .95),
        'max_ms_observed_only': max(observed) if observed else None,
        'failed_or_missing_or_slow_attempts': total - len(success),
        'status_counts': dict(Counter(item['status'] for item in attempts)),
        'failure_reason_counts': dict(Counter(reason for item in attempts for reason in item['failure_reasons'])),
    }


def aggregate(payload, manifest=None, evidence=None, target_ms=TARGET_MS):
    target_ms = validate_target(target_ms)
    records, metadata = unpack(payload)
    manifest = manifest or {}
    labels = manifest.get('attempts', manifest)
    if isinstance(labels, list):
        labels = {item['client_request_id']: item for item in labels}
    if not isinstance(labels, dict):
        raise ValueError('Manifest must map client_request_id to scenario/sample labels.')
    evidence = evidence or []
    if isinstance(evidence, dict):
        evidence = [evidence]
    evidence_events = [event for document in evidence for event in document.get('events', [])
                       if not metadata.get('session_id') or event.get('session_id') == metadata['session_id']]
    attempts, proactive = [], []
    ids = Counter(item.get('client_request_id') for item in records if isinstance(item, dict) and item.get('client_request_id'))
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            # A malformed submitted record remains a failed denominator entry.
            record = {'status': 'invalid', 'raw_invalid_record': record}
        request_id = record.get('client_request_id')
        if not request_id and record.get('submit_perf_ms') is None and record.get('turn_id'):
            proactive.append(record)
            continue
        label = labels.get(request_id, {})
        if not isinstance(label, dict):
            label = {}
        scenario = SCENARIO_BY_ID.get(label.get('scenario_id') or record.get('scenario_id'))
        if not scenario:
            scenario = SCENARIO_BY_QUERY.get(str(record.get('query', '')).rstrip('。'))
        observed_route = label.get('observed_route')
        if observed_route == 'task_progress' and scenario and scenario['semantic'] == 'delegate':
            scenario = {**scenario, 'semantic': 'task_progress'}
        errors, timings = [], {}
        submitted = record.get('submit_perf_ms')
        if not request_id or not record.get('turn_id') or not record.get('playback_id'):
            errors.append('missing_request_turn_playback_correlation')
        if request_id and ids[request_id] != 1:
            errors.append('duplicate_request_id')
        if not number(submitted):
            errors.append('invalid_browser_submit_clock')
        for name, field in TIME_FIELDS.items():
            value = record.get(field)
            delta = value - submitted if number(value) and number(submitted) else None
            timings[name] = round(delta, 3) if delta is not None and delta >= 0 else None
            if value is not None and timings[name] is None:
                errors.append(f'invalid_clock_order:{field}')
        if timings['first_nonzero_pcm_ms'] is None:
            errors.append('missing_real_nonzero_pcm')
        if timings['first_pcm_ms'] is None:
            errors.append('missing_real_pcm')
        if (timings['first_pcm_ms'] is not None and timings['first_nonzero_pcm_ms'] is not None
                and timings['first_nonzero_pcm_ms'] < timings['first_pcm_ms']):
            errors.append('nonzero_pcm_precedes_first_pcm')
        if not number(record.get('pcm_bytes')) or record['pcm_bytes'] < 2 or record['pcm_bytes'] % 2:
            errors.append('invalid_or_missing_pcm_bytes')
        if not number(record.get('pcm_packets')) or record['pcm_packets'] < 1:
            errors.append('invalid_or_missing_pcm_packets')
        semantic = semantic_errors(record, scenario, record.get('first_nonzero_pcm_perf_ms'), evidence_events)
        failure = sorted(set(errors + semantic))
        if record.get('status') != 'ended':
            failure.append(f"status:{record.get('status', 'missing')}")
        if record.get('context_state') != 'running':
            failure.append('audio_context_not_running')
        if timings['first_nonzero_pcm_ms'] is not None and timings['first_nonzero_pcm_ms'] > target_ms:
            failure.append('first_nonzero_pcm_exceeds_target')
        attempt = {
            'index': index, 'client_request_id': request_id,
            'turn_id': record.get('turn_id'), 'playback_id': record.get('playback_id'),
            'query': record.get('query'), 'scenario_id': scenario['scenario_id'] if scenario else None,
            'observed_route_override': observed_route,
            'purpose': label.get('purpose', manifest.get('default_purpose', 'unspecified')),
            'sample_phase': label.get('sample_phase', record.get('sample_phase', 'unlabelled')),
            'status': record.get('status', 'missing'), 'timings': timings,
            'measurement_errors': sorted(set(errors)), 'semantic_errors': semantic,
            'accepted': not failure, 'failure_reasons': sorted(set(failure)), 'raw': record,
        }
        attempts.append(attempt)
    groups = {}
    for key in ('scenario_id', 'sample_phase', 'purpose'):
        groups[key] = {str(value): summarize_attempts([item for item in attempts if item[key] == value], target_ms)
                       for value in dict.fromkeys(item[key] for item in attempts)}
    rendering = {}
    for timing in ('first_render_observed_ms', 'first_nonzero_render_observed_ms'):
        values = [item['timings'][timing] for item in attempts if item['timings'][timing] is not None]
        rendering[timing] = {'observations': len(values), 'attempts': len(attempts),
                             'p50_ms_observed_only': percentile(values, .5),
                             'p95_ms_observed_only': percentile(values, .95),
                             'max_ms_observed_only': max(values) if values else None}
    return {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'measurement': 'browser_submit_to_first_real_nonzero_pcm', 'target_ms': target_ms,
        'acceptance': f'Every submitted attempt must receive real nonzero PCM within {target_ms:g}ms and retain semantic correctness; P50 alone is insufficient.',
        'source_metadata': metadata, 'manifest': manifest,
        'actual_semantic_evidence': evidence,
        'summary': summarize_attempts(attempts, target_ms), 'groups': groups,
        'playback_observation': rendering,
        'attempts': attempts, 'proactive_audio_excluded_from_query_denominator': proactive,
        'limits': [
            'Only actual browser performance.now timestamps are used for end-to-end timing.',
            'P50/P95 describe observed timings; missing/error/cancelled/slow attempts remain in the acceptance denominator.',
            'First PCM enqueue is not playback. Worklet observation includes cross-thread notification delay and is not acoustic onset.',
            'First-query-after-restart does not prove cold provider, TLS or model connections; prewarming/reuse is recorded separately.',
            'Provider/model metadata is diagnostic, not substituted for browser timing.',
            'This offline aggregator trusts the recorded browser clock and cannot prove speaker identity or acoustic output.',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, help='Actual exported browser measurement JSON')
    parser.add_argument('--manifest', type=Path, help='Optional request-ID sampling labels')
    parser.add_argument('--evidence', type=Path, action='append', default=[], help='Actual trace export to verify an observed existing-task progress route')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--plan', action='store_true', help='Print a sampling plan; performs no requests')
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--target-ms', type=float, default=TARGET_MS,
                        help='Current acceptance target in ms (default: 1500); use 2000 to reassess historical batches.')
    args = parser.parse_args()
    try:
        validate_target(args.target_ms)
    except ValueError as exc:
        parser.error(str(exc))
    if args.plan:
        if args.repeats < 1:
            parser.error('--repeats must be positive')
        result = plan(args.repeats, args.target_ms)
    else:
        if not args.input:
            parser.error('--input is required unless --plan is selected')
        result = aggregate(json.loads(args.input.read_text()),
                           json.loads(args.manifest.read_text()) if args.manifest else None,
                           [json.loads(path.read_text()) for path in args.evidence], args.target_ms)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + '\n')
        print(json.dumps(result.get('summary', {'planned_attempts': result.get('planned_attempts')}), ensure_ascii=False))
    else:
        print(encoded)


if __name__ == '__main__':
    main()
