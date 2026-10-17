"""Lossless archive, compact model context: helpers only transform deep copies.

These functions never select an intent, truncate text, rewrite user constraints,
change a tool argument or modify the audit/DB record. The current vehicle
snapshot and offered tool schemas already supply state and capability details.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any
from backend.task_feedback import task_user_detail

TASK_FIELDS = ('id', 'goal', 'status', 'revision', 'question', 'constraints', 'progress', 'error_code')


def _vehicle_snapshot_delta(state: dict, baseline: dict, receipt: dict) -> dict | None:
    """Describe the whole receipt snapshot against an explicitly identified base.

    Paths are data, never tool-effect guesses. Arrays remain atomic and all
    changed fields (including unrelated device changes) survive reconstruction.
    """
    before = baseline.get('state') if isinstance(baseline, dict) else None
    if (not isinstance(before, dict) or type(baseline.get('revision')) is not int
            or not isinstance(baseline.get('observed_at'), str) or not baseline['observed_at']):
        return None
    changes, removed = [], []

    def compare(left, right, path):
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(left.keys() - right.keys()):
                removed.append([*path, key])
            for key in sorted(right):
                child = [*path, key]
                if key not in left:
                    changes.append({'path': child, 'value': deepcopy(right[key])})
                else:
                    compare(left[key], right[key], child)
        elif json.dumps(left, sort_keys=True, allow_nan=False) != json.dumps(right, sort_keys=True, allow_nan=False):
            changes.append({'path': path, 'value': deepcopy(right)})

    compare(before, state, [])
    encoded = json.dumps(before, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return {
        'receipt_time_scope': '本工具回执当时的整车状态；按以下差量从基准重建。历史回执不是本轮当前状态，基准时间不是回执时间。',
        'baseline': {'revision': baseline['revision'], 'observed_at': baseline['observed_at'],
                     'state_sha256': hashlib.sha256(encoded.encode()).hexdigest()},
        'receipt_identity': {key: deepcopy(receipt[key]) for key in
                             ('receipt_id', 'command_id', 'state_version', 'observed_at') if key in receipt},
        'set': changes, 'remove': removed,
    }


def _result_for_context(result: Any, read_details: bool) -> Any:
    if not isinstance(result, dict):
        return deepcopy(result)
    if read_details is True:
        return {key: deepcopy(value) for key, value in result.items() if key != 'tool_observations'}
    if isinstance(result.get('spoken_summary'), str) and result['spoken_summary'].strip():
        return {'spoken_summary': result['spoken_summary']}
    # Legacy tasks without a generated spoken summary retain their complete
    # answer. Omitting them would discard facts rather than remove duplication.
    if 'summary' in result:
        return {'summary': deepcopy(result['summary'])}
    return deepcopy(result)


def _task_for_context(task: Any, read_details: bool) -> Any:
    if not isinstance(task, dict):
        return deepcopy(task)
    compact = {key: deepcopy(task[key]) for key in TASK_FIELDS if key in task}
    compact['summary'] = task_user_detail(task)
    if 'result' in task:
        compact['result'] = ({'partial': True, 'spoken_summary': task_user_detail(task)}
                             if task.get('status') == 'failed' and task['result'] else
                             _result_for_context(task['result'], read_details))
    return compact


def compact_tool_result(body: Any, tool_name: str | None = None,
                        read_details: bool = False, *, vehicle_baseline: dict | None = None) -> Any:
    """Return an independent prompt copy of one structured tool result.

    Task conditions and status remain complete. Explicit detailed reads retain
    the complete result, while ordinary task references carry the generated
    spoken answer. Skill schemas are omitted only for a recognizable loaded
    manifest: the actual schemas are already offered through ``tools_for``.
    Ordinary tool values, reasons and nested payloads are never shortened.
    An explicitly supplied vehicle baseline may replace a full vehicle_state
    with a lossless, receipt-scoped delta, only when that representation is smaller.
    The archive and baseline are never modified; absent baselines keep snapshots.
    """
    result = deepcopy(body)
    if not isinstance(result, dict):
        return result
    result.pop('state', None)
    # Provider transport payloads remain in the audit transcript, not reasoning.
    result.pop('_provider_trace', None)
    result.pop('diagnostics', None)
    state = result.get('vehicle_state')
    if vehicle_baseline is not None and isinstance(state, dict) and 'vehicle_state_delta' not in result:
        delta = _vehicle_snapshot_delta(state, vehicle_baseline, result)
        encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        if delta is not None and len(encode({'vehicle_state_delta': delta})) < len(encode({'vehicle_state': state})):
            result.pop('vehicle_state')
            result['vehicle_state_delta'] = delta
    task = result.get('task')
    if isinstance(task, dict):
        result['task'] = _task_for_context(task, read_details)
    if isinstance(result.get('tasks'), list):
        result['tasks'] = [_task_for_context(task, read_details) for task in result['tasks']]
    if isinstance(result.get('items'), list):
        result['items'] = [compact_tool_result(item, tool_name, read_details, vehicle_baseline=vehicle_baseline)
                           for item in result['items']]
    skill = result.get('skill')
    manifest = (isinstance(skill, dict) and isinstance(skill.get('id'), str)
                and isinstance(skill.get('tool_names'), list)
                and all(isinstance(name, str) for name in skill['tool_names']))
    # Recognizing a catalog manifest also supports old tool messages that
    # don't carry their function name in the result body itself.
    if manifest and (tool_name in (None, 'load_skill')) and isinstance(result.get('tools'), list):
        result.pop('tools')
    return result


def compact_message(record: Any, tool_name: str | None = None,
                    read_details: bool = False, *, vehicle_baseline: dict | None = None) -> Any:
    """Compact valid JSON tool content only; every other message stays exact."""
    result = deepcopy(record)
    if not isinstance(result, dict) or result.get('role') != 'tool':
        return result
    content = result.get('content')
    if not isinstance(content, str):
        return result
    try:
        body = json.loads(content)
    except (ValueError, TypeError):
        return result
    result['content'] = json.dumps(compact_tool_result(body, tool_name, read_details, vehicle_baseline=vehicle_baseline),
                                   ensure_ascii=False, separators=(',', ':'))
    return result
