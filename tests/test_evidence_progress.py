"""Structural evidence and progress contracts; no model, network or language rules."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from backend.harness.evidence import (
    EvidenceReference, RetrievalLimits, RetrievalProgress, allow_retrieval,
    annotate_evidence, evidence_references, record_retrieval, retrieval_feedback,
)


def manual(text='某项设置仅支持停车时调整', *, version='v1'):
    return {'ok': True, 'manual': {'id': 'book', 'version': version, 'title': '选装功能说明'},
            'hits': [{'chunk_id': 4, 'page': 9, 'text': text, 'score': .9}], 'queried_at': 'first'}


def record(progress, result, invocation='one', tool='search_manual'):
    return record_retrieval(progress, invocation_id=invocation, tool_name=tool, result=result)


def test_same_sources_reordered_reranked_or_refetched_are_not_new_evidence():
    first = manual()
    first['hits'].append({'chunk_id': 5, 'page': 10, 'text': '该说明不适用于行驶状态', 'score': .8})
    before = deepcopy(first)
    progress, report = record(RetrievalProgress(), first)
    assert report.new_evidence_count == 2
    repeated = deepcopy(first)
    repeated.update(queried_at='later', query='不同表述', latency_ms=1)
    repeated['hits'].reverse()
    repeated['hits'][0]['score'] = .99
    progress, report = record(progress, repeated, 'two')
    assert report.new_evidence_count == 0
    assert report.consecutive_no_new_evidence == 1
    assert not allow_retrieval(progress, 'search_manual')
    assert allow_retrieval(progress, 'web_search')
    assert first == before


@pytest.mark.parametrize('updated', [manual('新版材料：仅车辆静止且驻车时支持'), manual(version='v2')])
def test_same_reference_with_new_content_or_source_version_is_progress(updated):
    progress, _ = record(RetrievalProgress(), manual())
    progress, report = record(progress, updated, 'two')
    assert report.new_evidence_count == 1
    assert report.consecutive_no_new_evidence == 0
    assert allow_retrieval(progress, 'search_manual')


def test_empty_or_failed_lookup_allows_one_reformulation_then_converges():
    for result in ({'ok': True, 'hits': []}, {'ok': False, 'code': 'unavailable'}):
        progress, report = record(RetrievalProgress(), result)
        assert report.new_evidence_count == 0 and allow_retrieval(progress, 'search_manual')
        progress, _ = record(progress, result, 'two')
        assert not allow_retrieval(progress, 'search_manual')
        feedback = retrieval_feedback(progress, 'search_manual')
        assert feedback['next_step'] == 'answer_with_available_evidence'
        assert feedback['executed'] is False


def test_progress_survives_json_checkpoint_and_duplicate_receipts_are_idempotent():
    progress, _ = record(RetrievalProgress(), manual())
    restored = RetrievalProgress.model_validate_json(progress.model_dump_json())
    duplicate, report = record(restored, manual())
    assert report.calls == 1 and duplicate == progress and duplicate is not progress
    duplicate, report = record(duplicate, manual(), 'two')
    restored = RetrievalProgress.model_validate_json(duplicate.model_dump_json())
    assert not allow_retrieval(restored, 'search_manual')
    assert report.total_calls == 2
    with pytest.raises(ValueError, match='Conflicting retrieval'):
        record(restored, manual('与原回执冲突'), 'two')


def test_total_and_per_tool_limits_remain_effective_despite_always_new_results():
    progress = RetrievalProgress()
    limits = RetrievalLimits(max_total_calls=3, max_calls_per_tool=2)
    for i in range(2):
        progress, _ = record(progress, manual(str(i)), str(i))
    assert not allow_retrieval(progress, 'search_manual', limits)
    assert allow_retrieval(progress, 'web_search', limits)
    progress, _ = record(progress, {'ok': True, 'results': []}, 'web', 'web_search')
    assert not allow_retrieval(progress, 'get_weather', limits)
    assert allow_retrieval(progress, 'set_volume', limits)


def test_web_query_specific_result_ids_and_scores_do_not_inflate_novelty():
    result = {'ok': True, 'results': [{'url': 'https://example.test/item', 'id': 'random-a',
                                    'title': '开放计划', 'content': '计划明年开放，具体时间待定', 'score': .9}]}
    progress, _ = record(RetrievalProgress(), result, tool='web_search')
    result['results'][0].update(id='random-b', score=.8)
    progress, report = record(progress, result, 'two', 'web_search')
    assert report.new_evidence_count == 0
    assert not report.support_assessed


def test_reading_full_page_is_distinct_from_a_search_snippet():
    search = {'ok': True, 'results': [{'url': 'https://example.test/item', 'content': '部分摘要'}]}
    extract = {'ok': True, 'results': [{'url': 'https://example.test/item', 'raw_content': '部分摘要及其他原文条件'}]}
    progress, _ = record(RetrievalProgress(), search, tool='search_web')
    progress, report = record(progress, extract, 'two', 'extract_web')
    assert report.new_evidence_count == 1
    assert progress.records[0].references[0].scope.kind == 'search_snippet'
    assert progress.records[1].references[0].scope.kind == 'page_content'


def test_annotation_carries_scope_without_claiming_answer_support_or_mutating_receipt():
    result = {'ok': True, 'mode': 'forecast', 'date': '2026-10-17', 'timezone': 'Asia/Shanghai',
              'place': {'name': '南京'}, 'summary': {'min_c': 12, 'max_c': 21},
              'details': {'interval': {'startTime': '2026-10-17T00:00:00+08:00', 'endTime': '2026-10-18T00:00:00+08:00'}}}
    original = deepcopy(result)
    annotated = annotate_evidence(result, 'get_weather')
    assert annotated['evidence_scope']['granularity'] == 'daily'
    assert annotated['evidence_scope']['coverage'] == 'partial'
    assert annotated['evidence_scope']['interval'] == result['details']['interval']
    assert annotated['answer_support_assessed'] is False
    assert result == original
    annotated['summary']['max_c'] = 99
    assert result['summary']['max_c'] == 21


def test_weather_refetch_timestamp_does_not_make_unchanged_values_new():
    result = {'ok': True, 'provider': 'fixture', 'place': {'name': '南京'}, 'date': '2026-10-17',
              'summary': {'min_c': 12, 'max_c': 21}, 'queried_at': 'first'}
    progress, _ = record(RetrievalProgress(), result, tool='get_weather')
    result['queried_at'] = 'later'
    progress, report = record(progress, result, 'two', 'get_weather')
    assert report.new_evidence_count == 0
    result['summary']['max_c'] = 22
    _, report = record(progress, result, 'three', 'get_weather')
    assert report.new_evidence_count == 1


def test_quick_lookup_retains_search_and_read_scope_separately():
    result = {'ok': True, 'search': {'results': [{'url': 'https://example.test/x', 'content': '摘要'}]},
              'extract': {'results': [{'url': 'https://example.test/x', 'raw_content': '完整原文'}]}}
    refs = evidence_references(result, 'lookup_web')
    assert {ref.scope.kind for ref in refs} == {'search_snippet', 'page_content'}
    assert len({ref.reference_id for ref in refs}) == 2


def test_progress_contract_rejects_forged_or_duplicate_fields():
    progress, _ = record(RetrievalProgress(), manual())
    data = progress.model_dump()
    data['records'].append(data['records'][0])
    with pytest.raises(ValidationError, match='unique'):
        RetrievalProgress.model_validate(data)
    with pytest.raises(ValidationError):
        RetrievalProgress(version=1, records=[], count=0)
    with pytest.raises(ValidationError):
        RetrievalLimits(max_no_new_evidence=0)
    with pytest.raises(ValidationError):
        EvidenceReference(source_id='x', version='1', content_sha256='not-a-hash', scope={})
    with pytest.raises(ValueError, match='Only retrieval'):
        record(RetrievalProgress(), {'ok': True}, tool='set_volume')


def test_mixed_search_and_read_annotation_does_not_label_every_reference_as_a_snippet():
    result = {'ok': True, 'search': {'results': [{'url': 'https://example.test/x', 'content': '摘要'}]},
              'extract': {'results': [{'url': 'https://example.test/x', 'raw_content': '完整原文'}]}}
    annotated = annotate_evidence(result, 'lookup_web')
    assert annotated['evidence_scope']['kind'] == 'unknown'
    assert {reference['kind'] for reference in annotated['evidence_refs']} == {'search_snippet', 'page_content'}


def test_unrecognized_manual_metadata_does_not_crash_receipt_accounting():
    result = {'ok': True, 'manual': 'provider description', 'hits': []}
    progress, report = record(RetrievalProgress(), result)
    assert len(progress.records) == 1 and report.new_evidence_count == 0
    assert annotate_evidence(result, 'search_manual')['evidence_refs'] == []
