"""Evidence scope and bounded retrieval progress, without language classification.

The caller persists the ledger with its checkpoint. Source identity, version and
content equality measure novelty, not relevance or whether a claim is supported.
Only accepted, resolved tool receipts belong here; unknown outcomes stay pending.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from super_xd.nlp.contracts.evidence import (RETRIEVAL_TOOLS, EvidenceScope, EvidenceReference, RetrievalRecord,
    RetrievalProgress, RetrievalLimits, EvidenceReport, _hash)



def _string(value):
    return value if isinstance(value, str) and value else None


def _scope(result, tool_name) -> EvidenceScope:
    kind = ('manual_excerpt' if tool_name == 'search_manual' else
            'page_content' if tool_name == 'extract_web' else
            'search_snippet' if tool_name in {'web_search', 'search_web'} else
            'unknown' if tool_name == 'lookup_web' else
            'weather' if tool_name in {'get_weather', 'get_weather_forecast'} else 'catalog')
    scope = EvidenceScope(kind=kind, coverage='partial', qualification=_string(result.get('notice')))
    if kind == 'manual_excerpt':
        manual = result.get('manual')
        scope.subject = _string(manual.get('title')) if isinstance(manual, dict) else None
    if kind == 'weather':
        place = result.get('place', result.get('location'))
        scope.subject = _string(place.get('name')) if isinstance(place, dict) else _string(place)
        scope.date, scope.timezone = _string(result.get('date')), _string(result.get('timezone'))
        granularity = result.get('granularity')
        summary = result.get('summary')
        daily = (tool_name == 'get_weather_forecast' or
                 isinstance(summary, dict) and {'min_c', 'max_c'} <= summary.keys() or
                 {'temperature_min', 'temperature_max'} <= result.keys())
        scope.granularity = (granularity if isinstance(granularity, str) and granularity in {'current', 'hourly', 'daily'} else
                             'current' if result.get('mode') == 'current' else 'daily' if daily else 'unknown')
        interval = (result.get('details') or {}).get('interval') if isinstance(result.get('details'), dict) else None
        if isinstance(interval, dict):
            scope.interval = {key: value for key, value in interval.items() if isinstance(value, str)}
    return scope


def evidence_references(result: dict[str, Any], tool_name: str) -> list[EvidenceReference]:
    """Normalize known provider shapes. Do not use query, rank, score or timing as evidence."""
    if tool_name not in RETRIEVAL_TOOLS or result.get('ok') is False:
        return []
    if tool_name == 'lookup_web':
        return [ref for field, name in (('search', 'search_web'), ('extract', 'extract_web'))
                if isinstance(result.get(field), dict) for ref in evidence_references(result[field], name)]
    scope = _scope(result, tool_name)
    rows = []
    if tool_name == 'search_manual':
        manual = result.get('manual') if isinstance(result.get('manual'), dict) else {}
        version = str(manual.get('version') or manual.get('id') or 'unspecified')
        for hit in result.get('hits') or []:
            if not isinstance(hit, dict) or not _string(hit.get('text')):
                continue
            source = str(manual.get('id') or manual.get('title') or 'manual')
            identity = hit.get('chunk_id', hit.get('page', _hash(hit['text'])))
            rows.append((source + ':' + str(identity), version, {'text': hit['text'], 'page': hit.get('page')}, scope))
    elif tool_name in {'web_search', 'search_web', 'extract_web'}:
        for row in result.get('results') or []:
            if not isinstance(row, dict):
                continue
            content = _string(row.get('raw_content')) or _string(row.get('content')) or _string(row.get('text'))
            url = _string(row.get('url'))
            if content and url:
                rows.append((url, str(row.get('version') or row.get('etag') or 'unspecified'),
                             {'title': row.get('title'), 'content': content}, scope))
    elif tool_name in {'get_weather', 'get_weather_forecast'}:
        values = {key: deepcopy(result[key]) for key in ('place', 'location', 'date', 'timezone', 'mode',
                  'summary', 'details', 'temperature_min', 'temperature_max', 'granularity') if key in result}
        if 'summary' in values or 'details' in values or 'temperature_min' in values:
            source = str(result.get('source_url') or result.get('provider') or tool_name)
            rows.append((source + ':' + str(scope.subject or '') + ':' + str(scope.date or ''),
                         str(result.get('version') or 'unspecified'), values, scope))
    else:
        key = 'places' if tool_name == 'search_places' else 'items'
        for row in result.get(key) or []:
            if not isinstance(row, dict):
                continue
            identity = row.get('place_ref') or row.get('place_id') or row.get('content_id')
            if not isinstance(identity, str) or not identity:
                continue
            values = {key: deepcopy(value) for key, value in row.items()
                      if key not in {'score', 'rank', 'queried_at', 'playback_url', 'stream_url', 'url', 'maps_url'}}
            rows.append((str(result.get('provider') or tool_name) + ':' + identity,
                         str(row.get('version') or 'unspecified'), values, scope))
    refs = {}
    for source, version, content, scope in rows:
        # Scope changes can change what an otherwise identical value supports.
        ref = EvidenceReference(source_id=source, version=version,
                                content_sha256=_hash({'content': content, 'scope': scope.model_dump()}), scope=scope)
        refs[ref.reference_id] = ref
    return list(refs.values())


def annotate_evidence(result: dict[str, Any], tool_name: str) -> dict[str, Any]:
    annotated = deepcopy(result)
    if tool_name in RETRIEVAL_TOOLS:
        annotated['evidence_scope'] = _scope(result, tool_name).model_dump(exclude_none=True)
        annotated['evidence_refs'] = [{'id': ref.reference_id, 'source_id': ref.source_id, 'version': ref.version, 'kind': ref.scope.kind}
                                      for ref in evidence_references(result, tool_name)]
        annotated['answer_support_assessed'] = False
    return annotated


def _report(progress: RetrievalProgress, tool_name: str) -> EvidenceReport:
    seen, calls, streak, new = set(), 0, 0, 0
    ids = []
    for record in progress.records:
        references = {ref.reference_id for ref in record.references}
        if record.tool_name == tool_name:
            new = len(references - seen)
            # One reformulation remains possible after an initial empty result.
            streak = streak + 1 if calls and not new else 0
            calls += 1
            ids = sorted(references)
        seen.update(references)
    return EvidenceReport(tool_name=tool_name, calls=calls, total_calls=len(progress.records),
                          new_evidence_count=new, consecutive_no_new_evidence=streak, evidence_ids=ids)


def record_retrieval(progress: RetrievalProgress, *, invocation_id: str, tool_name: str,
                     result: dict[str, Any]) -> tuple[RetrievalProgress, EvidenceReport]:
    if tool_name not in RETRIEVAL_TOOLS:
        raise ValueError('Only retrieval receipts belong in retrieval progress')
    record = RetrievalRecord(invocation_id=invocation_id, tool_name=tool_name,
                             receipt_sha256=_hash(result), references=evidence_references(result, tool_name))
    previous = next((item for item in progress.records if item.invocation_id == invocation_id), None)
    if previous is not None:
        if previous != record:
            raise ValueError('Conflicting retrieval receipt for an existing invocation')
        return progress.model_copy(deep=True), _report(progress, tool_name)
    updated = RetrievalProgress(records=[*(item.model_copy(deep=True) for item in progress.records), record])
    return updated, _report(updated, tool_name)


def allow_retrieval(progress: RetrievalProgress, tool_name: str,
                    limits: RetrievalLimits | None = None) -> bool:
    if tool_name not in RETRIEVAL_TOOLS:
        return True
    limits = limits or RetrievalLimits()
    report = _report(progress, tool_name)
    return (report.total_calls < limits.max_total_calls and report.calls < limits.max_calls_per_tool
            and report.consecutive_no_new_evidence < limits.max_no_new_evidence)


def retrieval_feedback(progress: RetrievalProgress, tool_name: str) -> dict[str, Any]:
    return {'ok': False, 'executed': False, 'code': 'retrieval_progress_exhausted',
            'next_step': 'answer_with_available_evidence', 'retry_model': True,
            'retrieval_progress': _report(progress, tool_name).model_dump()}
