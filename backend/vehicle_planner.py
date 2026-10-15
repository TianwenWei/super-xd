"""Pro-only vehicle plans. Validation/simulation never commits vehicle state.

The ledger proves structural bindings and declared constraints, not arbitrary
natural-language intent coverage. Real execution and receipts belong to actor.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import math
import logging
import unicodedata
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError

from backend.catalog import HOT_TOOL_NAMES, SKILLS, TOOLS, execute_tool, load_skill
from backend.models import sanitized_error, utcnow
from backend.prompt_templates import render_prompt
from backend.settings import VEHICLE_CONTROL_MODEL

MAX_MODEL_CALLS = 4
PLAN_FIELDS = ('status', 'intents', 'actions', 'keep_paths', 'question', 'reason')
PLAN_METADATA_FIELDS = {'ok', 'executed', 'loaded_skills', 'model_calls', 'plan_call_id', 'model_call_id',
                        'provenance', 'model_observations', 'load_evidence', 'trace_events', 'simulation',
                        'validation', 'action_ledger'}
ID = {'type': 'string', 'minLength': 1, 'maxLength': 100, 'pattern': r'[\s\S]*\S[\s\S]*'}
IDS = {'type': 'array', 'items': ID, 'uniqueItems': True, 'maxItems': 64}
PROMPT = render_prompt('vehicle_planner')


def _canonical(value):
    def normalize(item):
        if isinstance(item, float) and item.is_integer():
            return int(item)
        if isinstance(item, dict):
            return {key: normalize(child) for key, child in item.items()}
        if isinstance(item, list):
            return [normalize(child) for child in item]
        return item
    return json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _finite(value):
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(v) for v in value.values())
    if isinstance(value, list):
        return all(_finite(v) for v in value)
    return True


def _paths(value, prefix=''):
    result = []
    if prefix:
        result.append(prefix)
    if isinstance(value, dict):
        for key, child in value.items():
            result.extend(_paths(child, f'{prefix}.{key}' if prefix else str(key)))
    return result


def _value(state, path):
    node = state
    for key in path.split('.'):
        if not isinstance(node, dict) or key not in node:
            raise ValueError(f'unknown_state_path: {path}')
        node = node[key]
    return node


def _names(skills):
    return list(dict.fromkeys([*HOT_TOOL_NAMES, *(name for skill in skills for name in SKILLS[skill].tool_names)]))


def _object(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


def plan_schema(allowed_names, request_text, vehicle):
    """Dynamic per-name argument schema, using the actual loaded catalog."""
    state_paths = _paths(vehicle)
    keep = {'type': 'array', 'items': {'type': 'string', 'enum': state_paths}, 'uniqueItems': True, 'maxItems': 64}
    intent = _object({
        'id': ID, 'source_text': {'type': 'string', 'minLength': 1, 'maxLength': max(1, len(request_text))},
        'source_start': {'type': 'integer', 'minimum': 0, 'maximum': len(request_text)},
        'source_end': {'type': 'integer', 'minimum': 1, 'maximum': len(request_text)},
        'status': {'type': 'string', 'enum': ['planned', 'keep', 'clarification', 'refused', 'completed']},
        'action_ids': IDS, 'completed_operation_ids': IDS, 'keep_paths': keep,
        'question': {'type': 'string'}, 'reason': {'type': 'string'},
    })
    variants = [{'description': TOOLS[name].description,
                 **_object({'id': ID, 'intent_id': ID, 'name': {'type': 'string', 'enum': [name]},
                            'arguments': deepcopy(TOOLS[name].parameters), 'depends_on': IDS})} for name in allowed_names]
    return _object({'status': {'type': 'string', 'enum': ['planned', 'clarification', 'refused']},
                    'intents': {'type': 'array', 'items': intent, 'minItems': 1, 'maxItems': 32},
                    'actions': {'type': 'array', 'items': {'anyOf': variants}, 'maxItems': 48},
                    'keep_paths': keep, 'question': {'type': 'string'}, 'reason': {'type': 'string'}})


def _submission_schema(allowed_names, request_text, vehicle):
    """Compact provider wire schema; internal validation stays state-exact."""
    schema = plan_schema(allowed_names, request_text, vehicle)
    schema['properties']['keep_paths'] = deepcopy(schema['properties']['keep_paths'])
    intent = schema['properties']['intents']['items']
    for name in ('source_start', 'source_end'):
        intent['properties'].pop(name)
        intent['required'].remove(name)
    intent['properties']['source_text']['description'] = '逐字引用本轮原句中唯一出现的连续片段；不填下标，不增删文字或标点。'
    for keep in (schema['properties']['keep_paths'], intent['properties']['keep_paths']):
        keep['items'].pop('enum')
        keep['items']['minLength'] = 1
        keep['description'] = '只记录用户本轮显式保持的真实状态点路径；服务器校验路径。无显式保持则空。'
    schema['properties']['keep_paths']['description'] += '必须恰为各status=keep意图keep_paths的并集。'
    intent['properties']['keep_paths']['description'] += 'status=planned时必须为空；保持须独立status=keep意图。'
    return schema


def _resolve_source_spans(submission, request_text):
    """Add internal offsets only for a unique exact (including overlap) match.

    No original source text, action name, argument or constraint is rewritten.
    The unchanged strict internal validator checks the resulting full ledger.
    """
    resolved = deepcopy(submission)
    if not isinstance(resolved, dict) or not isinstance(resolved.get('intents'), list):
        raise ValueError('invalid_source_intent_ledger')
    for intent in resolved['intents']:
        if not isinstance(intent, dict) or not isinstance(intent.get('source_text'), str) or not intent['source_text'].strip():
            raise ValueError('intent_source_not_exact')
        if 'source_start' in intent or 'source_end' in intent:
            raise ValueError('wire_source_offsets_not_allowed')
        source, positions, cursor = intent['source_text'], [], 0
        while True:
            found = request_text.find(source, cursor)
            if found < 0:
                break
            positions.append(found)
            cursor = found + 1
        if not positions:
            raise ValueError('intent_source_not_exact')
        if len(positions) != 1:
            raise ValueError('intent_source_ambiguous: use a longer unique exact original substring')
        intent['source_start'] = positions[0]
        intent['source_end'] = positions[0] + len(source)
    return resolved


def _completed(operations):
    valid = {}
    for operation in operations or []:
        if not isinstance(operation, dict):
            continue
        receipt = operation.get('result') or {}
        if not isinstance(receipt, dict):
            continue
        identity = operation.get('operation_id') or operation.get('command_id')
        if (isinstance(identity, str) and identity and operation.get('command_id') == identity
                and operation.get('name') in TOOLS and receipt.get('ok') is True
                and isinstance(receipt.get('state'), dict)
                and receipt.get('name') == operation['name']
                and _canonical(receipt.get('arguments')) == _canonical(operation.get('arguments'))):
            if identity in valid:
                raise ValueError('duplicate_completed_operation_id')
            valid[identity] = deepcopy(operation)
    return valid


def _ordered(actions):
    by_id = {action['id']: action for action in actions}
    if len(by_id) != len(actions):
        raise ValueError('duplicate_action_id')
    for action in actions:
        if any(dependency not in by_id for dependency in action['depends_on']):
            raise ValueError('unknown_dependency')
    ordered, remaining, closed = [], list(actions), set()
    while remaining:
        ready = [action for action in remaining if set(action['depends_on']) <= closed]
        if not ready:
            raise ValueError('cyclic_dependency')
        for action in ready:
            ordered.append(action)
            closed.add(action['id'])
            remaining.remove(action)
    return ordered


def _write_paths(name, arguments, state):
    if TOOLS[name].state_mapping.get('operation') == 'read':
        return []
    paths = []
    for template in TOOLS[name].state_mapping['paths']:
        nodes = [('', state)]
        for part in template.split('.'):
            expanded = []
            for prefix, node in nodes:
                key = arguments.get(part[1:-1]) if part.startswith('{') and part.endswith('}') else part
                keys = list(node) if key in ('*', 'all') and isinstance(node, dict) else [str(key)]
                for child in keys:
                    expanded.append((f'{prefix}.{child}' if prefix else child,
                                     node.get(child) if isinstance(node, dict) else None))
            nodes = expanded
        paths.extend(path for path, _ in nodes)
    # catalog.temperature() rewrites all zones when the driver zone is synced,
    # even if the requested value already matches each zone's current value.
    if (name in {'set_temperature', 'adjust_temperature'} and arguments.get('zone') == 'driver'
            and state.get('climate', {}).get('sync_zones') is True):
        paths.extend(f'climate.temperatures.{zone}' for zone in state['climate']['temperatures'])
    return paths


def _overlap(a, b):
    return a == b or a.startswith(b + '.') or b.startswith(a + '.')


def validate_vehicle_action_keep(name, arguments, state, keep_paths):
    """Reject declared writes to held paths, including no-op writes.

    This does not prove the action preserves state: the actor must also compare
    the actual private candidate with its accepted keep baseline before commit.
    """
    if name not in TOOLS:
        raise ValueError(f'unknown_vehicle_tool: {name}')
    if not isinstance(arguments, dict) or not isinstance(keep_paths, (list, tuple, set)):
        raise ValueError('invalid_action_keep_contract')
    for keep in keep_paths:
        if not isinstance(keep, str) or not keep:
            raise ValueError('invalid_keep_path')
        _value(state, keep)
    for path in _write_paths(name, arguments, state):
        if any(_overlap(path, keep) for keep in keep_paths):
            raise ValueError(f'keep_path_write_conflict: {path}')


def _user_history(history, request_text):
    values = [item.get('content', '') for item in history or []
              if isinstance(item, dict) and item.get('role') == 'user' and isinstance(item.get('content'), str)]
    if values and values[-1] == request_text:
        values.pop()
    return values


def destination_grounded(destination, sources):
    """A destination must be copied from what the user (or a result) actually said.

    This is a provenance check on a structured argument, not place recognition:
    the model resolves references; code only refuses a name that appears in
    none of the supplied texts (whitespace ignored, as ASR may insert spaces).
    """
    compact = ''.join(str(destination or '').split())
    return bool(compact) and any(compact in ''.join(str(text or '').split()) for text in sources)


def validate_vehicle_plan(plan, request_text, vehicle, allowed_tools=None, completed_operations=None, history=None):
    """Return a validated private plan or raise ValueError; never execute live.

    Source-span coverage is only a text-accounting check. The model can still
    misunderstand a correctly quoted span; external intent gold remains needed.
    """
    if not isinstance(plan, dict) or not isinstance(request_text, str) or not request_text.strip():
        raise ValueError('invalid_plan_or_request')
    if set(plan) - set(PLAN_FIELDS) - PLAN_METADATA_FIELDS:
        raise ValueError('unexpected_plan_field')
    if not isinstance(vehicle, dict) or not _finite(vehicle) or not _finite(plan):
        raise ValueError('nonfinite_or_invalid_state')
    if allowed_tools is None:
        skills = plan.get('loaded_skills', [])
        if not isinstance(skills, list) or any(skill not in SKILLS for skill in skills):
            raise ValueError('invalid_loaded_skills')
        allowed = _names(skills)
    else:
        allowed = list(allowed_tools)
        if not allowed or any(name not in TOOLS for name in allowed):
            raise ValueError('invalid_allowed_tools')
    submitted = {key: deepcopy(plan.get(key)) for key in PLAN_FIELDS}
    for intent in submitted.get('intents') or []:
        if isinstance(intent, dict):
            intent.setdefault('completed_operation_ids', [])
            alias = intent.pop('text', intent.get('source_text'))
            if alias != intent.get('source_text'):
                raise ValueError('source_text_alias_mismatch')
    try:
        Draft202012Validator(plan_schema(allowed, request_text, vehicle)).validate(submitted)
    except ValidationError as exc:
        raise ValueError(f'plan_schema: {exc.message}') from None
    intents, actions = submitted['intents'], submitted['actions']
    by_intent = {intent['id']: intent for intent in intents}
    if len(by_intent) != len(intents):
        raise ValueError('duplicate_intent_id')
    spans, covered = set(), set()
    for intent in intents:
        start, end = intent['source_start'], intent['source_end']
        if not start < end or request_text[start:end] != intent['source_text'] or not intent['source_text'].strip():
            raise ValueError('intent_source_not_exact')
        if (start, end) in spans:
            raise ValueError('duplicate_intent_source_span')
        spans.add((start, end)); covered.update(range(start, end))
    needed = {i for i, character in enumerate(request_text)
              if not character.isspace() and unicodedata.category(character)[0] not in {'P', 'Z'}}
    uncovered = sorted(needed - covered)
    if uncovered:
        logging.getLogger(__name__).warning('Plan has %d unassigned source characters', len(uncovered))
    ordered = _ordered(actions)
    all_action_ids = {action['id'] for action in actions}
    for action in actions:
        if action['intent_id'] not in by_intent or by_intent[action['intent_id']]['status'] != 'planned':
            raise ValueError('action_has_no_planned_intent')
    completed = _completed(completed_operations)
    used_completed, seen_actions = set(), set()
    for action in actions:
        signature = _canonical([action['name'], action['arguments']])
        if signature in seen_actions:
            raise ValueError('duplicate_action')
        if any(signature == _canonical([operation['name'], operation['arguments']]) for operation in completed.values()):
            raise ValueError('already_completed_action')
        seen_actions.add(signature)
    declared_keep = set()
    for intent in intents:
        bound = {action['id'] for action in actions if action['intent_id'] == intent['id']}
        if set(intent['action_ids']) != bound or not set(intent['action_ids']) <= all_action_ids:
            raise ValueError('intent_action_binding_mismatch')
        status = intent['status']
        if status in {'planned', 'keep', 'completed'} and intent['question'].strip():
            raise ValueError('resolved_intent_has_unanswered_question')
        if status == 'planned' and (not bound or intent['keep_paths'] or intent['completed_operation_ids']):
            raise ValueError('planned_intent_missing_action_or_mixed_status')
        if status == 'keep' and (bound or not intent['keep_paths'] or intent['completed_operation_ids']):
            raise ValueError('keep_intent_missing_path_or_mixed_status')
        if status == 'completed':
            ids = intent['completed_operation_ids']
            if bound or intent['keep_paths'] or not ids or any(identity not in completed for identity in ids):
                raise ValueError('completed_intent_has_no_real_receipt')
            if set(ids) & used_completed:
                raise ValueError('completed_operation_reused')
            used_completed.update(ids)
        elif intent['completed_operation_ids']:
            raise ValueError('completed_receipt_bound_to_wrong_status')
        if status == 'clarification' and (not intent['question'].strip() or bound or intent['keep_paths']):
            raise ValueError('clarification_intent_not_explicit')
        if status == 'refused' and (not intent['reason'].strip() or bound or intent['keep_paths']):
            raise ValueError('refused_intent_not_explicit')
        declared_keep.update(intent['keep_paths'])
    if set(submitted['keep_paths']) != declared_keep:
        raise ValueError('keep_paths_not_bound_to_intents')
    if submitted['status'] == 'planned':
        if any(intent['status'] not in {'planned', 'keep', 'completed'} for intent in intents):
            raise ValueError('planned_with_unresolved_intent')
        if submitted['question'].strip():
            raise ValueError('planned_has_unanswered_question')
    elif actions or any(intent['status'] == 'planned' for intent in intents):
        raise ValueError('nonplanned_cannot_execute_partial_actions')
    elif submitted['status'] == 'clarification' and (not submitted['question'].strip() or not any(i['status'] == 'clarification' for i in intents)):
        raise ValueError('missing_clarification_question')
    elif submitted['status'] == 'refused' and (not submitted['reason'].strip() or not any(i['status'] == 'refused' for i in intents)):
        raise ValueError('missing_refusal_reason')
    candidate = deepcopy(vehicle)
    keep_before = {path: deepcopy(_value(vehicle, path)) for path in declared_keep}
    simulations = []
    for action in ordered:
        name, arguments = action['name'], action['arguments']
        validate_vehicle_action_keep(name, arguments, candidate, declared_keep)
        if name == 'set_navigation_destination' and not destination_grounded(
                arguments['destination'], [request_text, *_user_history(history, request_text)]):
            raise ValueError('destination_not_in_request: 目的地须逐字取自用户原话，不能改写或补全')
        outcome = execute_tool(name, arguments, candidate)
        if not outcome['ok']:
            raise ValueError(f'simulation_refused: {action["id"]}: {outcome.get("reason")}')
        if any(_canonical(_value(candidate, path)) != _canonical(value) for path, value in keep_before.items()):
            raise ValueError('simulation_changed_keep_path')
        simulations.append({'action_id': action['id'], 'intent_id': action['intent_id'], 'name': name,
                            'arguments': deepcopy(arguments), 'ok': True, 'simulated': True, 'executed': False,
                            'value': deepcopy(outcome.get('value'))})
    validated = deepcopy(plan)
    validated.update(submitted, actions=deepcopy(ordered), ok=submitted['status'] == 'planned', executed=False)
    for intent in validated['intents']:
        intent['text'] = intent['source_text']
    validated['simulation'] = {'executed': False, 'simulated': True, 'actions': simulations,
                               'final_state': candidate}
    validated['validation'] = {'submitted_action_order': [action['id'] for action in actions],
                               'execution_order': [action['id'] for action in ordered],
                               'structurally_valid': True, 'source_spans_accounted': not uncovered,
                               'warnings': [{'code': 'unaccounted_request_text', 'indices': uncovered}] if uncovered else [],
                               'semantic_coverage_proven': False}
    validated['action_ledger'] = [{'action_id': action['id'], 'intent_id': action['intent_id'],
                                  'status': 'planned', 'executed': False} for action in ordered]
    return validated


class VehicleControlPlanner:
    def __init__(self, ark):
        self.ark = ark

    async def plan(self, request_text, vehicle, skill_ids, history=None, completed_operations=None, trace=None):
        plan_call_id = uuid4().hex
        loaded, observations, loads, events = [], [], [], []
        model_call_id = None
        attempts = 0

        def emit(kind, data, *, call_id=None, status=None):
            event = {'kind': kind, 'data': deepcopy({'agent': 'vehicle_planner', **data}),
                     'call_id': call_id, 'status': status, 'created_at': utcnow()}
            events.append(event)
            if trace:
                trace(kind, deepcopy(event['data']), call_id=call_id, status=status)

        def finish(value):
            value = deepcopy(value)
            value.update(model_calls=attempts, loaded_skills=list(loaded), plan_call_id=plan_call_id,
                         model_call_id=model_call_id, model_observations=deepcopy(observations),
                         load_evidence=deepcopy(loads), trace_events=deepcopy(events), executed=False)
            value['provenance'] = {'plan_call_id': plan_call_id, 'model_call_id': model_call_id,
                                   'submit_call_id': value.pop('_submit_call_id', None), 'model': VEHICLE_CONTROL_MODEL,
                                   'executed': False}
            return value

        if (not isinstance(request_text, str) or not request_text.strip() or not isinstance(vehicle, dict) or not _finite(vehicle)
                or not isinstance(skill_ids, list) or not all(isinstance(skill, str) for skill in skill_ids)
                or len(skill_ids) != len(set(skill_ids))
                or any(skill not in SKILLS for skill in skill_ids)):
            return finish({'status': 'refused', 'ok': False, 'reason': 'invalid_planner_input',
                           'question': '', 'intents': [], 'actions': [], 'keep_paths': []})
        for skill in skill_ids:
            result = load_skill(skill)
            loads.append({'skill_id': skill, 'initial': True, 'result': result})
            loaded.append(skill)
            emit('vehicle.skill.loaded', loads[-1], call_id=plan_call_id, status='completed')
        planning_input = {
            'request_text': request_text, 'vehicle_snapshot': deepcopy(vehicle),
            'loaded_skills': list(loaded), 'available_action_names': _names(loaded),
            'history_reference_only': deepcopy(history or []), 'completed_operations': deepcopy(completed_operations or []),
            'skills': {key: manifest.model_dump() for key, manifest in SKILLS.items()},
            'capabilities': {'gps': False, 'real_navigation': False}, 'executed': False}
        messages = [{'role': 'system', 'content': PROMPT}, {'role': 'user', 'content': json.dumps(planning_input, ensure_ascii=False)}]
        for index in range(1, MAX_MODEL_CALLS + 1):
            attempts += 1
            allowed = _names(loaded)
            planning_input.update(loaded_skills=list(loaded), available_action_names=allowed)
            messages[1]['content'] = json.dumps(planning_input, ensure_ascii=False)
            submit = {'type': 'function', 'function': {'name': 'submit_vehicle_plan', 'strict': True,
                       'description': '提交整组结构化车辆计划；仅校验/模拟副本，不执行真实车辆。',
                       'parameters': _submission_schema(allowed, request_text, vehicle)}}
            loader = {'type': 'function', 'function': {'name': 'load_skill', 'strict': True,
                       'description': '仅为本Pro规划加载实际能力；不会执行，也不扩展Talker。',
                       'parameters': _object({'skill_id': {'type': 'string', 'enum': list(SKILLS)}})}}
            # Actual catalog schemas live in submit.actions.anyOf. Offering
            # them again as native tools would contradict the plan-only
            # protocol and invite an unnecessary refused execution attempt.
            tools = [loader, submit]
            model_call_id = uuid4().hex
            emit('model.started', {'plan_call_id': plan_call_id, 'model_call_index': index,
                                  'model': VEHICLE_CONTROL_MODEL, 'thinking': False, 'max_tokens': 2000,
                                  'messages': messages, 'tools': tools}, call_id=model_call_id, status='running')
            try:
                returned = await self.ark.complete(deepcopy(messages), deepcopy(tools),
                                                   model=VEHICLE_CONTROL_MODEL, thinking=False, max_tokens=2000)
            except asyncio.CancelledError:
                emit('model.cancelled', {'plan_call_id': plan_call_id, 'model_call_index': index,
                                        'received_message': False, 'executed': False},
                     call_id=model_call_id, status='cancelled')
                raise
            except Exception as exc:
                reason = sanitized_error(exc)
                emit('model.error', {'plan_call_id': plan_call_id, 'error': reason}, call_id=model_call_id, status='failed')
                return finish({'status': 'refused', 'ok': False, 'reason': reason, 'question': '',
                               'intents': [], 'actions': [], 'keep_paths': []})
            if not isinstance(returned, dict):
                observations.append({'model_call_id': model_call_id, 'raw_return': deepcopy(returned)})
                emit('model.error', {'plan_call_id': plan_call_id, 'error': 'invalid_model_message',
                                     'raw_return': returned}, call_id=model_call_id, status='failed')
                return finish({'status': 'refused', 'ok': False, 'reason': 'invalid_model_message', 'question': '',
                               'intents': [], 'actions': [], 'keep_paths': []})
            raw = deepcopy(returned)
            message = {key: deepcopy(value) for key, value in returned.items() if key != '_meta'}
            observations.append({'model_call_id': model_call_id, 'model_call_index': index,
                                 'message': message, 'meta': deepcopy(returned.get('_meta', {})),
                                 'raw_return': raw, 'loaded_skills': list(loaded)})
            emit('model.completed', {'plan_call_id': plan_call_id, 'model_call_index': index,
                                    'message': message, 'meta': returned.get('_meta', {})}, call_id=model_call_id, status='completed')
            messages.append(message)
            calls = message.get('tool_calls') or []
            if not calls:
                messages.append({'role': 'system', 'content': render_prompt('planner_retry')})
                continue
            submitted = None
            for call in calls:
                identity = call.get('id')
                fn = call.get('function') or {}; name = fn.get('name')
                emit('tool.started', {'plan_call_id': plan_call_id, 'name': name, 'arguments': fn.get('arguments'),
                                      'model_call_id': model_call_id}, call_id=identity, status='running')
                try:
                    arguments = json.loads(fn.get('arguments', '{}'))
                    if name == 'load_skill':
                        Draft202012Validator(loader['function']['parameters']).validate(arguments)
                        result = load_skill(arguments['skill_id'])
                        if arguments['skill_id'] not in loaded:
                            loaded.append(arguments['skill_id'])
                        loads.append({'skill_id': arguments['skill_id'], 'initial': False,
                                      'model_call_id': model_call_id, 'call_id': identity, 'result': deepcopy(result)})
                    elif name == 'submit_vehicle_plan':
                        if len(calls) != 1:
                            raise ValueError('submit_must_be_only_call_after_capabilities_loaded')
                        Draft202012Validator(submit['function']['parameters']).validate(arguments)
                        resolved = _resolve_source_spans(arguments, request_text)
                        emit('vehicle.plan.sources.resolved', {'plan_call_id': plan_call_id,
                             'binding_method': 'unique_exact_substring', 'model_call_id': model_call_id,
                             'bindings': [{'intent_id': intent['id'], 'source_text': intent['source_text'],
                                           'source_start': intent['source_start'], 'source_end': intent['source_end']}
                                          for intent in resolved['intents']]}, call_id=identity, status='completed')
                        submitted = validate_vehicle_plan(resolved, request_text, vehicle, allowed,
                                                          completed_operations, history)
                        submitted['_submit_call_id'] = identity
                        result = {'ok': True, 'executed': False, 'status': submitted['status'], 'validated': True}
                    else:
                        raise ValueError('plan_submission_required: 车辆函数不能在planner直接执行')
                except (ValueError, TypeError, KeyError, ValidationError) as exc:
                    result = {'ok': False, 'executed': False, 'error': str(exc), 'retry_model': True}
                emit('tool.completed' if result['ok'] else 'tool.error',
                     {'plan_call_id': plan_call_id, 'name': name, 'arguments': fn.get('arguments'), 'result': result,
                      'model_call_id': model_call_id}, call_id=identity, status='completed' if result['ok'] else 'failed')
                messages.append({'role': 'tool', 'tool_call_id': identity, 'content': json.dumps(result, ensure_ascii=False)})
            if submitted is not None:
                emit('vehicle.plan.validated', {'plan_call_id': plan_call_id, 'status': submitted['status'],
                                               'validation': submitted['validation'], 'executed': False},
                     call_id=model_call_id, status='completed')
                return finish(submitted)
        return finish({'status': 'refused', 'ok': False, 'reason': 'planner_model_step_limit', 'question': '',
                       'intents': [], 'actions': [], 'keep_paths': []})
