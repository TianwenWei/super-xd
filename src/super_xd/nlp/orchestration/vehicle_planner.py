"""Bounded model driver for vehicle planning; policy owns plan validation."""
from __future__ import annotations
import asyncio
from copy import deepcopy
import json
from uuid import uuid4
from jsonschema import Draft202012Validator, ValidationError
from super_xd.nlp.catalog.vehicle_plan import object_schema
from super_xd.nlp.prompts.catalog import SKILLS, TOOLS, load_skill
from super_xd.nlp.prompts.vehicle_plan import submission_schema
from super_xd.nlp.policy import vehicle as vehicle_policy
from super_xd.nlp.policy.speaker import DEFAULT_SPEAKER_SEAT, speaker_context
from super_xd.nlp.adapters.common import sanitized_error, utcnow
from super_xd.nlp.prompts.templates import render_prompt
from super_xd.nlp.prompts.config import config_text
from super_xd.nlp.release import get_release_id
from super_xd.nlp.engine.models import complete_model
from super_xd.nlp.contracts.providers import ModelRequest

MAX_MODEL_CALLS = 4
PROMPT = render_prompt('vehicle_planner')


class VehicleControlPlanner:
    def __init__(self, ark):
        self.ark = ark

    async def plan(self, request_text, vehicle, skill_ids, history=None, completed_operations=None, trace=None,
                   speaker_seat=DEFAULT_SPEAKER_SEAT, capabilities=None):
        plan_call_id = uuid4().hex
        loaded, observations, loads, events = [], [], [], []
        model_call_id = None
        attempts = 0
        request_policy = ModelRequest([], role='planner', reasoning=False, max_tokens=2000)
        effective = self.ark.resolve(request_policy) if hasattr(self.ark, 'resolve') else request_policy
        model_id, token_budget = effective.model, effective.max_tokens

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
                                   'submit_call_id': value.pop('_submit_call_id', None), 'model': model_id,
                                   'executed': False}
            return value

        if (not isinstance(request_text, str) or not request_text.strip() or not isinstance(vehicle, dict) or not vehicle_policy.finite_tree(vehicle)
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
            'speaker': speaker_context(speaker_seat),
            'loaded_skills': list(loaded), 'available_action_names': vehicle_policy.available_action_names(loaded),
            'history_reference_only': deepcopy(history or []), 'completed_operations': deepcopy(completed_operations or []),
            'skills': {key: manifest.model_dump() for key, manifest in SKILLS.items()},
            'capabilities': deepcopy(capabilities) if capabilities is not None else {'gps': False, 'real_navigation': False},
            'executed': False}
        # The stable rules stay first; dynamic device metadata belongs at the end.
        planning_message = {'role': 'user', 'content': json.dumps(planning_input, ensure_ascii=False)}
        messages = [{'role': 'system', 'content': PROMPT},
                    {'role': 'system', 'content': render_prompt('speaker_context',
                        speaker_json=json.dumps(planning_input['speaker'], ensure_ascii=False))}, planning_message]
        for index in range(1, MAX_MODEL_CALLS + 1):
            attempts += 1
            allowed = vehicle_policy.available_action_names(loaded)
            planning_input.update(loaded_skills=list(loaded), available_action_names=allowed,
                                  action_schemas={name: TOOLS[name].parameters for name in allowed})
            planning_message['content'] = json.dumps(planning_input, ensure_ascii=False)
            submit = {'type': 'function', 'function': {'name': 'submit_vehicle_plan',
                       'description': config_text('planner/tools.toml', 'submit_vehicle_plan.description'),
                       'parameters': submission_schema(allowed, request_text, vehicle)}}
            loader = {'type': 'function', 'function': {'name': 'load_skill',
                       'description': config_text('planner/tools.toml', 'load_skill.description'),
                       'parameters': object_schema({'skill_id': {'type': 'string', 'enum': list(SKILLS)}})}}
            # Catalog schemas travel as data (action_schemas). Offering them as
            # native tools would contradict the plan-only protocol and invite
            # an unnecessary refused execution attempt.
            tools = [loader, submit]
            model_call_id = uuid4().hex
            emit('model.started', {'plan_call_id': plan_call_id, 'model_call_index': index,
                                  'release_id': get_release_id(),
                                  'model': model_id, 'thinking': False, 'max_tokens': token_budget,
                                  'messages': messages, 'tools': tools}, call_id=model_call_id, status='running')
            try:
                completed = await complete_model(self.ark, ModelRequest(messages, tools, role='planner',
                                                 reasoning=False, max_tokens=token_budget))
            except asyncio.CancelledError as exc:
                emit('model.cancelled', {'plan_call_id': plan_call_id, 'model_call_index': index,
                                        'partial': deepcopy(getattr(exc, 'trace_partial', None)),
                                        'received_message': False, 'executed': False},
                     call_id=model_call_id, status='cancelled')
                raise
            except Exception as exc:
                reason = sanitized_error(exc)
                emit('model.error', {'plan_call_id': plan_call_id, 'error': reason,
                                     'partial': deepcopy(getattr(exc, 'trace_partial', None))},
                     call_id=model_call_id, status='failed')
                return finish({'status': 'refused', 'ok': False, 'reason': reason, 'question': '',
                               'intents': [], 'actions': [], 'keep_paths': []})
            raw = completed.raw_return
            message = completed.message
            observations.append({'model_call_id': model_call_id, 'model_call_index': index,
                                 'message': message, 'meta': deepcopy(completed.metadata),
                                 'raw_return': raw, 'loaded_skills': list(loaded)})
            emit('model.completed', {'plan_call_id': plan_call_id, 'model_call_index': index,
                                    'message': message, 'meta': completed.metadata, 'raw_return': raw}, call_id=model_call_id, status='completed')
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
                        resolved = vehicle_policy.resolve_source_spans(arguments, request_text)
                        emit('vehicle.plan.sources.resolved', {'plan_call_id': plan_call_id,
                             'binding_method': 'unique_exact_substring', 'model_call_id': model_call_id,
                             'bindings': [{'intent_id': intent['id'], 'source_text': intent['source_text'],
                                           'source_start': intent['source_start'], 'source_end': intent['source_end']}
                                          for intent in resolved['intents']]}, call_id=identity, status='completed')
                        submitted = vehicle_policy.validate_vehicle_plan(resolved, request_text, vehicle, allowed,
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
