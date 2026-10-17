"""Receipt-bound terminal expression shared by production, Demo and evaluation.

This module never executes an action or asks a model to speak. The accepted
observation ledger supplies facts; the frozen persona only renders those facts.
"""
from copy import deepcopy
import json
from string import Formatter

from jsonschema import Draft202012Validator

from backend.catalog import TOOLS
from backend.cockpit.tools import cockpit_feedback
from backend.persona import PERSONAS, render
from backend.speech_config import feedback_text, receipt_detail
from backend.speech_text import speech_text
from .diagnostics import safe_diagnostic
from .gateway_contracts import CompletionPresentation


_MISSING = object()


def _value(state, path):
    value = state
    for part in path.split('.'):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _vehicle_detail(name, arguments, result):
    """Read actual values when reporting settings, never echo a requested target."""
    generic = feedback_text('vehicle.confirmed')
    spec = TOOLS[name]
    state = result.get('vehicle_state') or result.get('state')
    if not isinstance(state, dict):
        return generic
    if result.get('domain') == spec.skill_id and spec.skill_id not in state:
        state = {spec.skill_id: state}
    args, actual = deepcopy(arguments), deepcopy(result)
    if name in {'set_temperature', 'adjust_temperature'}:
        temperatures = _value(state, 'climate.temperatures')
        if not isinstance(temperatures, dict) or not temperatures:
            return generic
        selected = temperatures if args['zone'] == 'all' else {args['zone']: temperatures.get(args['zone'])}
        if any(type(value) not in (int, float) for value in selected.values()):
            return generic
        actual['value'] = temperatures
        if name == 'adjust_temperature' or len(set(selected.values())) != 1:
            return receipt_detail('adjust_temperature', args, actual)
        args['temperature'] = next(iter(selected.values()))
    elif name in {'close_all_windows', 'open_all_windows', 'lock_all_doors', 'unlock_all_doors'}:
        values = _value(state, 'windows.positions' if 'windows' in name else 'locks.doors')
        expected = {'close_all_windows': 0, 'open_all_windows': 100,
                    'lock_all_doors': True, 'unlock_all_doors': False}[name]
        if not isinstance(values, dict) or not values or any(value != expected for value in values.values()):
            return generic
    else:
        paths = spec.state_mapping['paths']
        selectors = {field for path in paths for _, field, _, _ in Formatter().parse(path) if field}
        fields = set(spec.parameters['properties']) - selectors
        path = paths[0].format(**args)
        value = _value(state, path)
        if value is _MISSING and any(args.get(field) == 'all' for field in selectors):
            values = _value(state, path.rpartition('.')[0])
            if isinstance(values, dict) and values and all(item == next(iter(values.values())) for item in values.values()):
                value = next(iter(values.values()))
        if value is not _MISSING:
            actual['value'] = value
        if spec.state_mapping.get('operation') == 'set' and len(fields) == 1:
            field = next(iter(fields))
            if value is _MISSING or not Draft202012Validator(spec.parameters['properties'][field]).is_valid(value):
                return generic
            args[field] = deepcopy(value)
    try:
        return receipt_detail(name, args, actual)
    except (KeyError, TypeError, ValueError):
        # A real device need not implement the Demo's optional `value` shortcut.
        return generic


def _verified_details(checkpoint, groups):
    persona = PERSONAS[checkpoint.start.persona_id]
    details, seen = [], set()

    def control(identifier, name, arguments, observation):
        if identifier in seen or observation.status != 'completed':
            return
        seen.add(identifier)
        result = observation.result
        if result.get('ok') is not True or result.get('executed') is not True:
            return
        if name in TOOLS and TOOLS[name].state_mapping.get('operation') != 'read':
            detail = _vehicle_detail(name, arguments, result)
        elif name == 'control_media' and arguments.get('action') != 'get_status':
            detail = cockpit_feedback('media_done')
        elif name == 'start_poi_navigation' or name == 'manage_navigation' and arguments.get('action') not in {'get_status', 'route'}:
            detail = cockpit_feedback('navigation_done')
        else:
            return
        details.append(render(persona, 'success', detail))

    for group in groups:
        for item in group.requests:
            request = item.request
            observation = group.results.get(request.invocation_id) or group.receipts.get(request.invocation_id)
            if observation is None or observation.status != 'completed':
                continue
            result = observation.result
            if result.get('ok') is not True:
                continue
            if request.name == 'delegate_task' and result.get('registered') is True and isinstance(result.get('task'), dict):
                details.append(render(persona, 'delegated', request.arguments.get('spoken_goal') or request.arguments['goal']))
            elif request.name == 'manage_task' and request.arguments.get('action') in {'cancel', 'update', 'reply'} and isinstance(result.get('task'), dict):
                details.append(render(persona, 'cancel' if request.arguments['action'] == 'cancel' else 'updated'))
            else:
                control(request.invocation_id, request.name, request.arguments, observation)
        for receipt in [*group.plan_receipts, *(receipt for plan in group.completed_plans for receipt in plan.receipts)]:
            control(receipt.invocation_id, receipt.name, receipt.arguments, receipt)
    return list(dict.fromkeys(details))


def _json_object(value):
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _unannounced_groups(checkpoint):
    """Bind each group's own preface by its native calls and accepted receipts.

    Model tool-call IDs may repeat across steps; rejected proposals are present
    in the transcript but absent from orchestration. Ordered receipt matching
    keeps either case from suppressing another group's verified facts.
    """
    proposals, current = [], None
    for message in checkpoint.run.messages:
        if message.get('role') == 'assistant':
            current = None
            native = message.get('tool_calls')
            if not isinstance(native, list) or not native:
                continue
            calls = []
            for call in native:
                function = call.get('function') if isinstance(call, dict) else None
                arguments = _json_object(function.get('arguments')) if isinstance(function, dict) else None
                if arguments is None or not isinstance(call.get('id'), str) or not isinstance(function.get('name'), str):
                    break
                calls.append((call['id'], function['name'], arguments))
            if len(calls) != len(native):
                continue
            content = message.get('content')
            current = (calls, {}, isinstance(content, str) and bool(content.strip()))
            proposals.append(current)
        elif message.get('role') == 'tool' and current is not None:
            result = _json_object(message.get('content'))
            if result is not None and isinstance(message.get('tool_call_id'), str):
                current[1][message['tool_call_id']] = result
    selected, before = [], len(proposals)
    for group in reversed(checkpoint.orchestration.groups):
        calls = [(item.request.tool_call_id, item.request.name, item.request.arguments) for item in group.requests]
        receipts = {item.request.tool_call_id: group.results[item.request.invocation_id].result
                    for item in group.requests if item.request.invocation_id in group.results}
        announced = False
        for index in range(before - 1, -1, -1):
            if proposals[index][:2] == (calls, receipts):
                announced, before = proposals[index][2], index
                break
        if not announced:
            selected.append(group)
    return list(reversed(selected))


def completion_presentation(checkpoint, error_details=None):
    """Only accepted terminal facts produce speech; unknown/cancelled stay silent."""
    status = checkpoint.run.status
    if status not in {'completed', 'failed'}:
        return None
    if status == 'completed':
        last_assistant = next((message for message in reversed(checkpoint.run.messages)
                               if message.get('role') == 'assistant'), {})
        # A final answer covers the turn. A tool group's preface covers only
        # that group; earlier unannounced receipts still need their completion.
        content = last_assistant.get('content')
        if not last_assistant.get('tool_calls') and isinstance(content, str) and content.strip():
            return None
    groups = _unannounced_groups(checkpoint) if status == 'completed' else checkpoint.orchestration.groups
    details = _verified_details(checkpoint, groups)
    if status == 'failed':
        structured = error_details or {}
        reason = structured.get('spoken_reason')
        if not isinstance(reason, str) or not reason.strip():
            code = checkpoint.run.error_code
            if code in {'unsupported_capability', 'not_configured'}:
                reason = cockpit_feedback('unsupported_capability')
            elif code in {'task_commit_unknown', 'execution_not_confirmed', 'authoritative_vehicle_state_required'}:
                reason = feedback_text('errors.outcome_unknown')
            elif code in {'model_or_protocol_error', 'model_timeout', 'upstream_error', 'auth', 'permission_denied', 'rate_limit'}:
                reason = feedback_text('errors.model_connection_pending' if details else 'errors.model_connection')
            else:
                reason = feedback_text('errors.operation_incomplete')
        details.append(render(PERSONAS[checkpoint.start.persona_id], 'error', safe_diagnostic(reason)))
    if not details:
        return None
    return CompletionPresentation(speech=speech_text(' '.join(details)),
        source='verified_receipts' if status == 'completed' or len(details) > 1 else 'error_template')
