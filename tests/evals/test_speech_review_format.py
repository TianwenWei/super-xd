"""Reviewer formatting is accepted narrowly; invalid reviews never become scores."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from super_xd.nlp.textutil import decode_json_object
from evals import speech_review


def verdict(**changes):
    return {'preface_claims_completion': False, 'natural': 4, 'issue': '', **changes}


def test_shared_format_helper_has_no_speech_verdict_semantics():
    assert decode_json_object('```json\n{"nested": {"items": [1, null, true]}}\n```') == {
        'nested': {'items': [1, None, True]}}
    with pytest.raises(ValueError, match='Duplicate'):
        decode_json_object('{"nested": {"same": 1, "same": 2}}')
    with pytest.raises(ValueError, match='Nonfinite'):
        decode_json_object('{"nested": [1e999]}')


@pytest.mark.parametrize('wrap', [lambda value: value, lambda value: f'```json\n{value}\n```',
                                  lambda value: f'~~~~JSON\n{value}\n~~~~'])
def test_complete_object_or_single_closed_json_fence_retains_raw(wrap):
    raw = '\n' + wrap(json.dumps(verdict(issue='语气可以更自然。'), ensure_ascii=False)) + '\n'
    row = speech_review.parse_review_response(raw, preface='这就处理。')
    assert row['review_status'] == 'reviewed'
    assert row['raw_response'] == raw
    assert row['natural'] == 4 and row['issue'] == '语气可以更自然。'


@pytest.mark.parametrize('raw', [
    None, '', '[]', 'null', 'true', '{"natural": 5}',
    '说明\n```json\n{}\n```', '```json\n{}\n```\n说明',
    '```json\n{}\n```\n```json\n{}\n```', '```json\n{}',
    '```json\n{}\n~~~', '```json\n{}\n    ```', '```python\n{}\n```',
    '```\n{}\n```', '```json\n{}\n``` trailing',
    '[hidden]: https://example.test\n\n```json\n{}\n```',
    '```json\n{}\n```\n\n[hidden]: https://example.test',
    '{} {}', '{"preface_claims_completion": false, "natural": 1, "natural": 5, "issue": ""}',
    '{"preface_claims_completion": false, "natural": NaN, "issue": ""}',
])
def test_malformed_or_mixed_material_is_not_extracted_or_scored(raw):
    row = speech_review.parse_review_response(raw, preface='这就处理。')
    assert row['review_status'] == 'parse_error'
    assert row['parse_error'] and row['raw_response'] == raw
    assert 'natural' not in row
    assert speech_review.summarize_reviews([row]) == {
        'turns': 1, 'reviewed': 0, 'unreviewed': 1, 'parse_errors': 1, 'provider_errors': 0,
        'scored': 0, 'mean_natural': None, 'prefaces': 0, 'completion_claims': 0}


@pytest.mark.parametrize('changes', [
    {'natural': True}, {'natural': '5'}, {'natural': 0}, {'natural': 6},
    {'natural': float('inf')}, {'natural': 1e300}, {'issue': None}, {'issue': []},
    {'preface_claims_completion': 'false'}, {'preface_claims_completion': 0},
    {'preface_claims_completion': None}, {'review_status': 'reviewed'},
])
def test_required_verdict_values_are_validated(changes):
    row = speech_review.parse_review_response(json.dumps(verdict(**changes)), preface='这就处理。')
    assert row['review_status'] == 'parse_error' and 'natural' not in row


def test_no_preface_requires_null_but_valid_low_score_remains_an_advisory_review():
    invalid = speech_review.parse_review_response(json.dumps(verdict()), preface='')
    valid = speech_review.parse_review_response(json.dumps(verdict(preface_claims_completion=None, natural=1)), preface='')
    assert invalid['review_status'] == 'parse_error'
    assert valid['review_status'] == 'reviewed'
    assert speech_review.summarize_reviews([valid])['mean_natural'] == 1


def test_reparse_preserves_original_response_and_does_not_mutate_or_invent_reviews():
    raw = '```json\n' + json.dumps(verdict()) + '\n```'
    source = {'model_configuration': {'source': 'fixture'}, 'summary': {'scored': 0}, 'reviews': [
        {'user': 'fixture', 'preface': '正在处理。', 'full_reply': '正在处理。', 'parse_error': raw,
         'response_metadata': {'usage': {'total_tokens': 20}}},
        {'user': 'other', 'error': 'provider unavailable'}]}
    original = deepcopy(source)
    report = speech_review.reparse_report(source)
    assert source == original
    first, second = report['reviews']
    assert first['review_status'] == 'reviewed' and first['raw_response'] == raw
    assert first['previous_review'] == {'parse_error': raw}
    assert first['response_metadata'] == source['reviews'][0]['response_metadata']
    assert second['review_status'] == 'unreviewed' and 'natural' not in second
    assert report['summary']['reviewed'] == 1 and report['summary']['unreviewed'] == 1


@pytest.mark.parametrize('valid', [True, False])
async def test_cli_reviewer_format_failure_is_nonzero_and_saved(monkeypatch, tmp_path, valid):
    source, output = tmp_path / 'source.json', tmp_path / 'review.json'
    source.write_text(json.dumps({'results': [{'turns': [{'input': 'fixture', 'preface': '正在处理。', 'text': '正在处理。'}]}]}))
    raw = '```json\n' + json.dumps(verdict(natural=1)) + '\n```' if valid else 'model did not return a verdict'
    closed = []

    async def complete(request):
        return SimpleNamespace(message={'role': 'assistant', 'content': raw}, metadata={'fixture': True})

    async def close():
        closed.append(True)

    llm = SimpleNamespace(resolve=lambda request: SimpleNamespace(model='fixture-model'),
        config=SimpleNamespace(source='fixture', role=lambda role: {'profile': 'fixture'},
                               document={'llm': {'profiles': {'fixture': {'provider': 'fixture'}}}}),
        providers={'fixture': SimpleNamespace(complete=complete)}, aclose=close)
    monkeypatch.setattr(speech_review, 'create_llm', lambda: llm)
    status = await speech_review.main([str(source), '--out', str(output)])
    report = json.loads(output.read_text())
    assert status == (0 if valid else 1) and closed == [True]
    assert report['reviews'][0]['raw_response'] == raw
    assert report['summary']['unreviewed'] == (0 if valid else 1)
    assert report['summary']['mean_natural'] == (1 if valid else None)
