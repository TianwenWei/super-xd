"""Latency reports retain missing output and classify only first-call evidence."""
from copy import deepcopy

import pytest

from evals.harness import latency_summary


def turn(identity, *, first=100, total=400, error=None, route='lookup'):
    return {'turn_id': identity, 'route': route, 'first_effective_ms': first,
            'total_ms': total, 'error': error, 'model_calls': 2,
            'usage': {'prompt_tokens': 200, 'cached_tokens': 100}}


def completion(identity, usage, *, agent=None):
    data = {'meta': {'usage': usage}}
    if agent is not None:
        data['agent'] = agent
    return {'kind': 'model.completed', 'turn_id': identity, 'data': data}


def cached(value):
    return {'prompt_tokens': 200, 'prompt_tokens_details': {'cached_tokens': value}}


def test_first_talker_cache_evidence_survives_tool_continuations_and_other_roles():
    result = {'turns': [turn('cold'), turn('warm', first=200, total=800, route='vehicle')], 'traces': [
        completion('cold', cached(90), agent='vehicle_planner'),
        completion('cold', cached(0)), completion('cold', cached(100)),
        completion('warm', cached(50), agent='talker'), completion('warm', cached(0)),
    ]}
    original = deepcopy(result)
    summary = latency_summary([result])
    all_ = summary['all']
    assert all_['turns'] == all_['first_text_samples'] == 2
    assert all_['no_first_text_turns'] == all_['error_turns'] == 0
    assert all_['cached_tokens'] == 200, 'Aggregate token accounting stays separate from cold/warm classification.'
    groups = all_['cache_groups']
    assert groups['zero_cache']['turns'] == groups['cache_hit']['turns'] == 1
    assert groups['unknown']['turns'] == 0
    assert groups['zero_cache']['first_text_p50'] == groups['zero_cache']['first_text_p90'] == 100
    assert groups['cache_hit']['total_p50'] == groups['cache_hit']['total_p90'] == 800
    assert summary['lookup']['cache_groups']['zero_cache']['turns'] == 1
    assert summary['vehicle']['cache_groups']['cache_hit']['turns'] == 1
    assert result == original


def test_timeout_without_completion_is_counted_unknown_even_when_partial_trace_reports_usage():
    result = {'turns': [turn('timeout', first=None, total=10000, error='first_output_deadline'),
                        turn('failed-after-text', first=90, total=190, error='upstream_error')], 'traces': [
        {'kind': 'model.started', 'turn_id': 'timeout', 'data': {}},
        {'kind': 'model.error', 'turn_id': 'timeout',
         'data': {'partial': {'meta': {'usage': cached(0)}}}},
        completion('failed-after-text', cached(0)),
    ]}
    summary = latency_summary([result])['all']
    assert summary['turns'] == summary['error_turns'] == 2
    assert summary['first_text_samples'] == summary['no_first_text_turns'] == 1
    assert summary['first_text_p50'] == summary['first_text_p90'] == 90
    unknown = summary['cache_groups']['unknown']
    assert unknown['turns'] == unknown['no_first_text_turns'] == unknown['error_turns'] == 1
    assert unknown['first_text_samples'] == 0
    assert unknown['first_text_p50'] is None and unknown['first_text_p90'] is None
    assert unknown['total_p50'] == unknown['total_p90'] == 10000
    assert summary['cache_groups']['zero_cache']['error_turns'] == 1


@pytest.mark.parametrize('usage', [None, {}, {'prompt_tokens': 200}, cached(None), cached(False), cached(-1)])
def test_missing_or_invalid_first_usage_stays_unknown_after_later_cache_hit(usage):
    result = {'turns': [turn('missing')], 'traces': [
        completion('other-turn', cached(0)), completion('missing', usage), completion('missing', cached(80)),
    ]}
    groups = latency_summary([result])['all']['cache_groups']
    assert groups['unknown']['turns'] == groups['unknown']['first_text_samples'] == 1
    assert groups['zero_cache']['turns'] == groups['cache_hit']['turns'] == 0


def test_old_failed_turn_without_total_or_trace_is_not_dropped():
    summary = latency_summary([{'turns': [{'error': 'turn_timeout'}]}])['all']
    assert summary['turns'] == summary['no_first_text_turns'] == summary['error_turns'] == 1
    assert summary['total_p50'] is None
    assert summary['cache_groups']['unknown']['turns'] == 1


def test_explicit_flat_cached_usage_is_evidence_and_cache_groups_do_not_cross_results():
    results = [
        {'turns': [turn('same-id')], 'traces': [completion('same-id', {'cached_tokens': 0})]},
        {'turns': [turn('same-id')], 'traces': []},
    ]
    groups = latency_summary(results)['all']['cache_groups']
    assert groups['zero_cache']['turns'] == groups['unknown']['turns'] == 1
    assert groups['cache_hit']['turns'] == 0


def test_model_output_latency_excludes_fallback_and_receipt_presentations_without_guessing_legacy_data():
    timeout = {**turn('timeout', first=10000, total=10010, error='timeout'),
               'first_model_text_ms': None, 'presentation_source': 'error_template'}
    partial = {**turn('partial', first=250, total=900, error='upstream_error'),
               'first_model_text_ms': 250, 'presentation_source': 'error_template'}
    control = {**turn('control', first=600, total=610),
               'first_model_text_ms': None, 'presentation_source': 'verified_receipts'}
    legacy = turn('legacy', first=100)
    result = {'turns': [timeout, partial, control, legacy], 'traces': [
        completion('control', cached(0)), completion('legacy', cached(50))]}
    summary = latency_summary([result])['all']
    assert summary['first_text_samples'] == 4 and summary['no_first_text_turns'] == 0
    assert summary['model_text_samples'] == 1
    assert summary['no_model_text_turns'] == 2
    assert summary['model_text_unknown_turns'] == 1
    assert summary['first_model_text_p50'] == summary['first_model_text_p90'] == 250
    assert summary['cache_groups']['zero_cache']['no_model_text_turns'] == 1
    assert summary['cache_groups']['cache_hit']['model_text_unknown_turns'] == 1
    assert summary['cache_groups']['unknown']['model_text_samples'] == 1
    assert 'first_model_text_ms' not in legacy
