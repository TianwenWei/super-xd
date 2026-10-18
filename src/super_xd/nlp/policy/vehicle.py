"""Pure vehicle plan validation against source spans and accepted receipts."""
from __future__ import annotations
from copy import deepcopy
import json
import math
import logging
import unicodedata
from jsonschema import Draft202012Validator, ValidationError
from super_xd.nlp.catalog.vehicle import HOT_TOOL_NAMES, SKILLS, TOOLS, action_write_paths
from super_xd.nlp.catalog.vehicle_plan import PLAN_FIELDS, PLAN_METADATA_FIELDS, plan_schema


def canonical_action_value(value):
    def normalize(item):
        if isinstance(item, float) and item.is_integer():
            return int(item)
        if isinstance(item, dict):
            return {key: normalize(child) for key, child in item.items()}
        if isinstance(item, list):
            return [normalize(child) for child in item]
        return item
    return json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)



def finite_tree(value):
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(finite_tree(v) for v in value.values())
    if isinstance(value, list):
        return all(finite_tree(v) for v in value)
    return True



def state_value(state, path):
    node = state
    for key in path.split('.'):
        if not isinstance(node, dict) or key not in node:
            raise ValueError(f'unknown_state_path: {path}')
        node = node[key]
    return node



def available_action_names(skills):
    return list(dict.fromkeys([*HOT_TOOL_NAMES, *(name for skill in skills for name in SKILLS[skill].tool_names)]))



def resolve_source_spans(submission, request_text):
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
                and canonical_action_value(receipt.get('arguments')) == canonical_action_value(operation.get('arguments'))):
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



def _overlap(a, b):
    return a == b or a.startswith(b + '.') or b.startswith(a + '.')



def validate_vehicle_action_keep(name, arguments, state, keep_paths):
    """Reject declared writes to held paths, including no-op writes.

    This does not prove the action preserves state: the executing device must
    enforce the accepted keep baseline and return its actual state in the receipt.
    """
    if name not in TOOLS:
        raise ValueError(f'unknown_vehicle_tool: {name}')
    if not isinstance(arguments, dict) or not isinstance(keep_paths, (list, tuple, set)):
        raise ValueError('invalid_action_keep_contract')
    for keep in keep_paths:
        if not isinstance(keep, str) or not keep:
            raise ValueError('invalid_keep_path')
        state_value(state, keep)
    for path in action_write_paths(name, arguments, state):
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
    if not isinstance(vehicle, dict) or not finite_tree(vehicle) or not finite_tree(plan):
        raise ValueError('nonfinite_or_invalid_state')
    if allowed_tools is None:
        skills = plan.get('loaded_skills', [])
        if not isinstance(skills, list) or any(skill not in SKILLS for skill in skills):
            raise ValueError('invalid_loaded_skills')
        allowed = available_action_names(skills)
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
        signature = canonical_action_value([action['name'], action['arguments']])
        if signature in seen_actions:
            raise ValueError('duplicate_action')
        if any(signature == canonical_action_value([operation['name'], operation['arguments']]) for operation in completed.values()):
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
    for action in ordered:
        name, arguments = action['name'], action['arguments']
        validate_vehicle_action_keep(name, arguments, vehicle, declared_keep)
        if name == 'set_navigation_destination' and not destination_grounded(
                arguments['destination'], [request_text, *_user_history(history, request_text)]):
            raise ValueError('destination_not_in_request: 目的地须逐字取自用户原话，不能改写或补全')
    validated = deepcopy(plan)
    validated.update(submitted, actions=deepcopy(ordered), ok=submitted['status'] == 'planned', executed=False)
    for intent in validated['intents']:
        intent['text'] = intent['source_text']
    validated['validation'] = {'submitted_action_order': [action['id'] for action in actions],
                               'execution_order': [action['id'] for action in ordered],
                               'structurally_valid': True, 'executor_validation_required': True,
                               'source_spans_accounted': not uncovered,
                               'warnings': [{'code': 'unaccounted_request_text', 'indices': uncovered}] if uncovered else [],
                               'semantic_coverage_proven': False}
    validated['action_ledger'] = [{'action_id': action['id'], 'intent_id': action['intent_id'],
                                  'status': 'planned', 'executed': False} for action in ordered]
    return validated
