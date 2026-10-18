"""Pure Talker business rules and preparation from caller-owned, factual state.

Prepared commands are proposals, never execution results. A later action is
prepared again after its predecessor's actual receipt, without advancing state
from a proposed command. This module owns no clients, scheduler or persistence.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
import json
from typing import Any

from jsonschema import Draft202012Validator

from super_xd.nlp.catalog.vehicle import HOT_TOOL_NAMES
from super_xd.nlp.policy.cockpit import finite, observed_media_state, prepare_media_action, prepare_navigation, scoped_place, validated_receipt
from super_xd.nlp.contracts.media import CockpitError, media_item
from super_xd.nlp.prompts.speech import feedback_text
from super_xd.nlp.catalog.talker import talker_tools
from super_xd.nlp.contracts.gateway_v1 import NLP_TARGETS


TERMINAL_TASKS = {'completed', 'failed', 'cancelled'}
VALIDATORS = {item['function']['name']: Draft202012Validator(item['function']['parameters']) for item in talker_tools()}


class ProductPolicyError(ValueError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        recoverable = code in {
            'area_not_grounded', 'invalid_task_selection', 'task_selection', 'task_text_required',
            'invalid_place', 'unknown_place', 'place_not_confirmed', 'invalid_action',
            'weather_quick_required', 'destination_not_in_request', 'origin_required',
            'planned_vehicle_group_conflict', 'navigation_group_conflict', 'unknown_content',
        } or details.get('needs_clarification') is True
        self.result = {'ok': False, 'executed': False, 'code': code, 'reason': message,
                       'retry_model': recoverable, **details}


def _mapping(value):
    return value.model_dump() if hasattr(value, 'model_dump') else deepcopy(dict(value))


def grounded(value, sources):
    """Check source copying, never classify language or infer user intent."""
    compact = ''.join(str(value or '').split())
    return bool(compact) and any(compact in ''.join(str(source or '').split()) for source in sources)


def normalize_search_arguments(arguments, sources):
    result = deepcopy(arguments)
    parts = [part for part in result.pop('area').split() if grounded(part, sources)]
    if not parts:
        raise ProductPolicyError('area_not_grounded', 'Search area has no supplied source', retry_model=True)
    result['near'] = ' '.join(parts)
    return result


def resolve_place_reference(reference, places):
    if not isinstance(reference, str) or not reference.strip():
        return None
    if reference in places:
        return reference
    matches = [key for key, place in places.items()
               if place.get('place_id') == reference or (place.get('details') or {}).get('place_id') == reference]
    return matches[0] if len(matches) == 1 else None


def resolve_task(tasks, task_id=None):
    """The same unique-active-task selection used by the demo actor."""
    if not tasks:
        return None, {'ok': False, 'code': 'no_task', 'retry_model': True, 'executed': False,
                      'reason': '本会话尚无后台任务，不能manage_task。请根据本轮原句重新选择直接回答或实际可用功能；车控缺功能时delegate_vehicle_control，不要创建无关后台任务。'}
    if task_id:
        found = next((task for task in tasks if task['id'] == task_id), None)
        return found, None if found else {'ok': False, 'reason': feedback_text('task.not_found')}
    active = [task for task in tasks if task['status'] not in TERMINAL_TASKS]
    if len(active) == 1:
        return active[0], None
    if not active and len(tasks) == 1:
        return tasks[0], None
    return None, {'ok': False, 'needs_clarification': True, 'reason': feedback_text('task.select'),
                  'candidates': [{key: task[key] for key in ('id', 'goal', 'status')} for task in (active or tasks)[-8:]]}


def normalize_task_arguments(arguments, tasks):
    result = deepcopy(arguments)
    identities = result.get('task_ids')
    if identities is not None:
        if result['action'] != 'get' or result.get('task_id') is not None:
            raise ProductPolicyError('invalid_task_selection', 'task_ids only supports batch get')
        if not 1 <= len(identities) <= 8 or len(set(identities)) != len(identities) or any(not item.strip() for item in identities):
            raise ProductPolicyError('invalid_task_selection', 'Select one to eight distinct nonempty task IDs')
    else:
        if result.get('task_id') is not None and not result['task_id'].strip():
            raise ProductPolicyError('invalid_task_selection', 'Task ID cannot be blank')
        identities = [result.get('task_id')]
    selected = []
    for identifier in identities:
        task, error = resolve_task(tasks, identifier)
        if error:
            details = {key: value for key, value in error.items() if key not in {'code', 'reason', 'ok', 'executed'}}
            raise ProductPolicyError(error.get('code', 'task_selection'), error['reason'], **details)
        selected.append(task)
    if 'task_ids' not in result:
        task = selected[0]
        result['task_id'] = task['id']
        if result['action'] != 'get':
            if task['status'] in {'cancelling', 'cancelled'} or result['action'] == 'cancel' and task['status'] in TERMINAL_TASKS:
                raise ProductPolicyError('task_terminal', 'Task cannot accept this mutation in its current state')
            if result['action'] != 'cancel' and not result.get('text', '').strip():
                raise ProductPolicyError('task_text_required', 'Task reply/update requires text')
            result['expected_revision'] = task['revision']
    return result


def _places(context):
    return {item['place_ref']: {**deepcopy(item.get('details') or {}), **deepcopy(item)} for item in context.get('places', [])}


def _physical_place(place):
    return {key: deepcopy(value) for key, value in place.items()
            if key not in {'details', 'source_turn_id', 'presented_to_user', 'selected_by_user'}}


def _route_points(origin, destination, waypoints):
    systems = set()
    for place in (origin, destination, *waypoints):
        if not isinstance(place, dict) or not isinstance(place.get('coordinate_system'), str):
            raise ProductPolicyError('unsupported_coordinate_system', 'Route points require an explicit coordinate system')
        systems.add(place['coordinate_system'].upper())
        location = place.get('location', place)
        if not isinstance(location, dict):
            raise ProductPolicyError('invalid_place', 'Route coordinates are invalid')
        latitude, longitude = location.get('latitude'), location.get('longitude')
        if not finite(latitude) or not finite(longitude) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ProductPolicyError('invalid_place', 'Route coordinates are invalid')
    if len(systems) != 1:
        raise ProductPolicyError('unsupported_coordinate_system', 'Route points use incompatible coordinate systems')


def _place(reference, context, turn_id, *, presented=True):
    places = _places(context)
    reference = resolve_place_reference(reference, places)
    if reference is None:
        raise ProductPolicyError('unknown_place', 'Place is absent or its provider ID is ambiguous', retry_model=True)
    place = places[reference]
    if presented and (place.get('presented_to_user') is not True or place.get('source_turn_id') == turn_id):
        raise ProductPolicyError('place_not_confirmed', 'POI must come from a previously presented result', retry_model=True)
    return reference, place


def _sources(start, context):
    values = [start['user_text']]
    location = context.get('environment', {}).get('location')
    if location:
        values.extend((location['name'], location.get('country', '')))
    for turn in start.get('history', [])[-4:]:
        for record in turn['records']:
            if record['role'] in {'user', 'assistant'}:
                values.append(turn.get('heard_text', '') if record['role'] == 'assistant' and turn.get('interrupted_playback') else record.get('content') or '')
    values.extend(item['name'] for item in context.get('places', []))
    return values


def _domain(name, arguments):
    if name == 'control_media':
        return 'media'
    if name in {'manage_navigation', 'start_poi_navigation'}:
        return 'navigation'
    return None


def _effective_request(request, context):
    if isinstance(context.get('cockpit', {}).get('media'), dict):
        if request['name'] == 'set_volume':
            return {**request, 'name': 'control_media', 'arguments': {'action': 'volume', 'volume': request['arguments']['volume']}}
        if request['name'] == 'control_playback':
            action = request['arguments']['action']
            return {**request, 'name': 'control_media', 'arguments': {'action': 'resume' if action == 'play' else action}}
    return request


def _mutation(name, arguments):
    return name == 'start_poi_navigation' or name in {'control_media', 'manage_navigation'} and arguments.get('action') not in {'get_status', 'route'}


def _check_action_fields(request):
    name, arguments = request['name'], request['arguments']
    action = arguments.get('action')
    if name == 'control_media':
        required = {'play': 'content_id', 'seek': 'position_seconds', 'volume': 'volume'}.get(action)
        if required and required not in arguments:
            raise ProductPolicyError('invalid_action', 'Media action requires ' + required)
        if action in {'seek', 'volume'}:
            number = arguments[required]
            if not finite(number) or number < 0 or action == 'volume' and number > 100:
                raise ProductPolicyError('invalid_action', 'Media action value is out of range')
    if name == 'manage_navigation':
        required = ('place_ref' if action in {'start', 'add_waypoint', 'set_origin'} else
                    'waypoint_ref' if action == 'remove_waypoint' else None)
        if required and not arguments.get(required):
            raise ProductPolicyError('invalid_action', 'Navigation action requires ' + required)


def _updates_domain_context(name, arguments):
    return _mutation(name, arguments) or name in {'control_media', 'manage_navigation'} and arguments.get('action') == 'get_status'


def _state(context, domain):
    state = context.get('cockpit', {}).get(domain)
    if not isinstance(state, dict) or type(state.get('version')) is not int or state['version'] < 0:
        raise ProductPolicyError('state_required', f'A versioned {domain} state is required')
    return state


def _prepare(start, context, request):
    request = _effective_request(request, context)
    _check_action_fields(request)
    name, arguments = request['name'], deepcopy(request['arguments'])
    output = {'execution_arguments': arguments, 'domain': _domain(name, arguments), 'prepared_command': None}
    if name == 'manage_task':
        output['execution_arguments'] = normalize_task_arguments(arguments, context.get('tasks', []))
    elif name == 'delegate_task':
        if arguments.get('query_domain') == 'weather' and arguments.get('query_mode') != 'quick':
            raise ProductPolicyError('weather_quick_required', 'Weather tasks require explicit quick mode',
                                     required_query_mode='quick')
    elif name == 'set_navigation_destination':
        if not grounded(arguments['destination'], _sources(start, context)):
            raise ProductPolicyError('destination_not_in_request', 'Destination has no supplied source', retry_model=True)
    elif name == 'search_places':
        output['execution_arguments'] = normalize_search_arguments(arguments, _sources(start, context))
    elif name in {'start_poi_navigation', 'manage_navigation'}:
        action = 'start' if name == 'start_poi_navigation' else arguments['action']
        normalized = {'action': action, 'place_ref': arguments['place_id']} if name == 'start_poi_navigation' else arguments
        if action in {'start', 'add_waypoint', 'set_origin'}:
            normalized['place_ref'], _ = _place(normalized.get('place_ref'), context, start['identity']['turn_id'])
        if normalized.get('origin_ref'):
            normalized['origin_ref'], _ = _place(normalized['origin_ref'], context, start['identity']['turn_id'])
        if action == 'get_status':
            output['execution_arguments'] = normalized
            return output
        state = _state(context, 'navigation')
        if action == 'route':
            if not state.get('destination'):
                raise ProductPolicyError('unknown_place', 'No navigation destination')
            origin = _places(context).get(normalized.get('origin_ref')) or state.get('origin') or context['vehicle']['state'].get('_meta', {}).get('coordinates')
            if not origin:
                raise ProductPolicyError('origin_required', 'Choose an actual route origin')
            waypoints = state.get('waypoints', [])
            _route_points(origin, state['destination'], waypoints)
            output['execution_arguments'] = {'origin': _physical_place(origin), 'destination': _physical_place(state['destination']),
                                             'waypoints': [_physical_place(item) for item in waypoints]}
        else:
            if action == 'remove_waypoint' and normalized.get('waypoint_ref'):
                aliases = {item['place_ref']: item for item in state.get('waypoints', [])}
                normalized['waypoint_ref'] = resolve_place_reference(normalized['waypoint_ref'], aliases)
            output['execution_arguments'] = normalized
            output['prepared_command'] = prepare_navigation(action, normalized, state,
                {key: _physical_place(place) for key, place in _places(context).items()})
    elif name == 'control_media':
        action = arguments['action']
        if action != 'get_status':
            state = _state(context, 'media')
            choices = {item['content_id']: item for item in context.get('media', [])}
            command = prepare_media_action(action, arguments, state, choices)
            if command['expected'].get('kind') == 'video' and command['expected'].get('status') == 'playing' and start['speaker_seat'] == 'driver':
                speed = context['vehicle']['state'].get('_meta', {}).get('speed_kph')
                if not finite(speed) or speed != 0:
                    raise ProductPolicyError('video_driving', 'Driver video requires a known stationary vehicle')
            output['prepared_command'] = command
            if action in {'next', 'previous'}:
                output['execution_arguments']['content_id'] = command['expected']['content_id']
    if output['prepared_command'] is not None:
        domain = output['domain']
        output['prepared_command']['expected_state_version'] = _state(context, domain)['version']
    return output


def _apply_result(start, context, request, result):
    if result.get('ok') is not True:
        return
    request = _effective_request(request, context)
    name, arguments = request['name'], request['arguments']
    if (name in HOT_TOOL_NAMES or name == 'delegate_vehicle_control') and result.get('vehicle_state') is not None:
        vehicle = result['vehicle_state']
        if not isinstance(vehicle, dict) or any(not isinstance(item, dict) for item in vehicle.values()):
            raise ProductPolicyError('invalid_vehicle_state', 'Vehicle receipt requires a structured actual snapshot')
        context['vehicle']['state'] = deepcopy(vehicle)
    if name == 'search_media':
        items = result.get('items')
        if not isinstance(items, list) or len(items) > arguments.get('max_results', 5):
            raise ProductPolicyError('invalid_content', 'Media catalog receipt is invalid')
        choices = []
        for value in items:
            if not isinstance(value, dict):
                raise ProductPolicyError('invalid_content', 'Media catalog receipt contains a non-object item')
            item = media_item({**value, 'provider': value.get('provider') or result.get('provider')})
            if item['kind'] != arguments['kind'] or not item.get('provider'):
                raise ProductPolicyError('invalid_content', 'Media receipt changed kind or omitted provider')
            choices.append({**item, 'source_turn_id': start['identity']['turn_id'], 'presented_to_user': False})
        if len({item['content_id'] for item in choices}) != len(choices):
            raise ProductPolicyError('invalid_content', 'Media catalog contains duplicate IDs')
        context['media'] = choices
    elif name == 'search_places':
        values = result.get('places')
        if not isinstance(values, list) or len(values) > arguments.get('max_results', 5):
            raise ProductPolicyError('invalid_place', 'Place search receipt is invalid')
        places = {item['place_ref']: item for item in context.get('places', [])}
        for value in values:
            place = scoped_place(value, result.get('provider'))
            places[place['place_ref']] = {'place_ref': place['place_ref'], 'name': place['name'], 'provider': place['provider'],
                                         'details': place, 'source_turn_id': start['identity']['turn_id'], 'presented_to_user': False}
        context['places'] = list(places.values())[-50:]
    elif _mutation(name, arguments):
        if result.get('executed') is not True:
            raise ProductPolicyError('execution_not_confirmed', 'A control receipt must confirm actual execution')
        prepared = _prepare(start, context, request)['prepared_command']
        domain = prepared['domain']
        state = result.get('state')
        receipt = {**result, 'state_version': result.get('state_version', state.get('version') if isinstance(state, dict) else None)}
        command = {**prepared, 'wire': {'domain': domain, 'expected_state_version': prepared['expected_state_version']}}
        context.setdefault('cockpit', {})[domain] = validated_receipt(command, receipt)
    elif name == 'control_media' and arguments['action'] == 'get_status':
        previous = context.get('cockpit', {}).get('media') or {'version': -1}
        context.setdefault('cockpit', {})['media'] = observed_media_state(previous, result)
    elif name == 'manage_navigation' and arguments['action'] == 'get_status':
        state = result.get('state')
        previous = context.get('cockpit', {}).get('navigation') or {'version': -1}
        version = result.get('state_version', state.get('version') if isinstance(state, dict) else None)
        if not isinstance(state, dict) or type(version) is not int or version < max(0, previous['version']):
            raise ProductPolicyError('stale_receipt', 'Invalid navigation state version')
        if type(state.get('active')) is not bool or not isinstance(state.get('waypoints'), list) or len(state['waypoints']) > 10:
            raise ProductPolicyError('invalid_navigation_state', 'Navigation status is incomplete')
        if any(item is not None and (not isinstance(item, dict) or not item.get('place_ref'))
               for item in [state.get('destination'), state.get('origin'), *state['waypoints']]):
            raise ProductPolicyError('invalid_navigation_state', 'Navigation status lacks place identities')
        context.setdefault('cockpit', {})['navigation'] = {**deepcopy(previous), **deepcopy(state), 'version': version}
    elif name in {'delegate_task', 'manage_task'}:
        tasks = {item['id']: item for item in context.get('tasks', [])}
        selected = normalize_task_arguments(arguments, list(tasks.values())) if name == 'manage_task' else None
        allowed = set(selected.get('task_ids') or [selected['task_id']]) if selected is not None else None
        for task in result.get('tasks', []) or ([result['task']] if isinstance(result.get('task'), dict) else []):
            if (not isinstance(task, dict) or not isinstance(task.get('id'), str) or not task['id']
                    or type(task.get('revision')) is not int or task['revision'] < 1
                    or task.get('status') not in TERMINAL_TASKS | {'queued', 'running', 'waiting_clarification', 'cancelling', 'awaiting_confirm'}
                    or not isinstance(task.get('goal'), str) or not task['goal']):
                raise ProductPolicyError('invalid_task_receipt', 'Task receipt lacks versioned task state')
            if allowed is not None and task['id'] not in allowed:
                raise ProductPolicyError('invalid_task_receipt', 'Task receipt returned an unselected task')
            known = tasks.get(task['id'])
            if known is None or task['revision'] > known['revision'] or task['revision'] == known['revision'] and (
                    known['status'] not in TERMINAL_TASKS or task['status'] == known['status']):
                tasks[task['id']] = deepcopy(task)
        context['tasks'] = list(tasks.values())


def product_context(input_mapping, observations_mapping=None):
    """Replay only accepted tool receipts, never model assertions or proposals."""
    value = _mapping(input_mapping)
    start, checkpoint = value['start'], value['checkpoint']
    context = deepcopy(start['context'])
    calls = {}
    for message in checkpoint['messages'][len(checkpoint['prepared']['messages']):]:
        if message.get('role') == 'assistant':
            calls = {call['id']: {'name': call['function']['name'], 'arguments': json.loads(call['function']['arguments'])}
                     for call in message.get('tool_calls', [])}
        elif message.get('role') == 'tool' and message.get('tool_call_id') in calls:
            _apply_result(start, context, calls.pop(message['tool_call_id']), json.loads(message['content']))
    observations = {key: _mapping(item) for key, item in (observations_mapping or {}).items()}
    for request in value.get('requests', checkpoint.get('pending', [])):
        observation = observations.get(request['invocation_id'])
        if observation is None:
            continue
        if observation['identity'] != start['identity'] or observation['invocation_id'] != request['invocation_id']:
            raise ProductPolicyError('stale_receipt', 'Observation belongs to another invocation or context')
        if observation['status'] == 'completed':
            _apply_result(start, context, request, observation['result'])
    return context


def prepare_product_action(input_mapping, request_mapping, observations_mapping=None):
    value, request = _mapping(input_mapping), _mapping(request_mapping)
    effective = _effective_request(request, value['start']['context'])
    domain = _domain(effective['name'], effective['arguments'])
    prior_observations = {}
    for previous in value.get('requests', value['checkpoint'].get('pending', [])):
        if previous['invocation_id'] == request['invocation_id']:
            break
        observation = (observations_mapping or {}).get(previous['invocation_id'])
        if observation is not None:
            prior_observations[previous['invocation_id']] = observation
        effective = _effective_request(previous, value['start']['context'])
        if _domain(effective['name'], effective['arguments']) == domain and _updates_domain_context(effective['name'], effective['arguments']):
            if observation is None or _mapping(observation)['status'] == 'unknown':
                raise ProductPolicyError('pending_receipt', 'Prepare this action after the preceding actual receipt')
    context = product_context(value, prior_observations)
    return _prepare(value['start'], context, request)


class ProductTalkerPolicy:
    """Full domain validation; adapters still own execution and receipt identity."""

    def __init__(self, bindings: Mapping[str, Any], *, enabled_nlp_targets: Sequence[str] = ()):
        self.bindings = {key: _mapping(value) for key, value in bindings.items()}
        self.enabled_nlp_targets = frozenset(enabled_nlp_targets)
        if not self.enabled_nlp_targets <= set(NLP_TARGETS.values()):
            raise ValueError('Unknown NLP orchestration port')
        for binding in self.bindings.values():
            if binding.get('kind') != 'tool_execution' or binding.get('operation') not in {'query', 'control'} or binding.get('execution_location') not in {'cloud', 'device'}:
                raise ValueError('Tool bindings require explicit operation and execution location')
        config = {'bindings': self.bindings, 'enabled_nlp_targets': sorted(self.enabled_nlp_targets)}
        self.policy_id = 'product-talker-v1-' + hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def binding_key(request):
        name, arguments = request['name'], request['arguments']
        suffix = arguments.get('kind') if name == 'search_media' else arguments.get('action') if name in {'control_media', 'manage_navigation'} else None
        return name + ('.' + suffix if suffix else '')

    def _binding(self, value, request):
        name, arguments = request['name'], request['arguments']
        if name not in VALIDATORS:
            raise ProductPolicyError('unknown_tool', 'Talker tool is not available')
        VALIDATORS[name].validate(arguments)
        if name in NLP_TARGETS:
            target = NLP_TARGETS[name]
            if target not in self.enabled_nlp_targets:
                raise ProductPolicyError('not_configured', 'NLP orchestration port is not configured')
            return {'kind': 'nlp_orchestration', 'target': target}
        operation = 'query' if name in {'search_places', 'get_weather', 'web_search', 'search_manual', 'search_media'} or name in {'control_media', 'manage_navigation'} and arguments['action'] in {'get_status', 'route'} else 'control'
        binding = self.bindings.get(self.binding_key(request))
        if binding is None or binding['operation'] != operation or operation == 'control' and binding['execution_location'] != 'device':
            raise ProductPolicyError('not_configured', 'No compatible server-owned tool binding')
        action = arguments.get('action') if name in {'control_media', 'manage_navigation'} else None
        kind = arguments.get('kind') if name == 'search_media' else None
        available = any(item['name'] == name and item.get('available', True) and item.get('action') == action
                        and item.get('content_kind') == kind and item['operation'] == operation
                        and item['execution_location'] == binding['execution_location']
                        for item in value['start']['context']['capabilities']['tools'])
        if not available:
            raise ProductPolicyError('unsupported_capability', 'The context does not advertise this capability')
        return deepcopy(binding)

    def prepare_product_action(self, input_mapping, request_mapping, observations_mapping=None):
        value, request = _mapping(input_mapping), _mapping(request_mapping)
        route = self._binding(value, request)
        try:
            prepared = prepare_product_action(value, request, observations_mapping)
        except CockpitError as error:
            raise ProductPolicyError(error.code, str(error)) from error
        if route['kind'] == 'nlp_orchestration':
            return {**route, 'execution_arguments': prepared['execution_arguments']}
        if prepared['domain'] is None:
            prepared['domain'] = route.get('domain')
        return {**route, **prepared, 'depends_on': []}

    def validate_observations(self, input_mapping, observations_mapping):
        return product_context(input_mapping, observations_mapping)

    def validate_group(self, input_):
        value = _mapping(input_)
        requests = value['requests']
        names = [item['name'] for item in requests]
        if names.count('delegate_vehicle_control') and (names.count('delegate_vehicle_control') > 1 or
                any(item['name'] in HOT_TOOL_NAMES or _mutation(item['name'], item['arguments']) for item in requests)):
            raise ProductPolicyError('planned_vehicle_group_conflict', 'A group cannot mix planning with direct control')
        destinations = sum(item['name'] in {'set_navigation_destination', 'start_poi_navigation'} or
                           item['name'] == 'manage_navigation' and item['arguments'].get('action') == 'start' for item in requests)
        if destinations > 1:
            raise ProductPolicyError('navigation_group_conflict', 'A group cannot select multiple destinations')
        output, previous = [], {}
        context = product_context(value)
        for request in requests:
            route = self._binding(value, request)
            effective = _effective_request(request, context)
            _check_action_fields(effective)
            domain = _domain(effective['name'], effective['arguments'])
            dependencies = [previous[domain]] if domain in previous else []
            if dependencies:
                # Validate source identities before any part of the group leaves
                # NLP; state-dependent constraints are rechecked after receipts.
                if effective['name'] == 'manage_navigation':
                    arguments = effective['arguments']
                    for field in ('place_ref', 'origin_ref'):
                        if arguments.get(field):
                            _place(arguments[field], context, value['start']['identity']['turn_id'])
                if effective['name'] == 'control_media' and effective['arguments']['action'] == 'play':
                    known = {item['content_id'] for item in context.get('media', [])}
                    state = context.get('cockpit', {}).get('media', {})
                    known.update(item['content_id'] for item in state.get('queue', []))
                    known.add(state.get('content_id'))
                    if effective['arguments'].get('content_id') not in known:
                        raise ProductPolicyError('unknown_content', 'Content ID has no supplied catalog source')
                # The actual state after this dependency is not available yet.
                # The scheduler must call prepare_product_action after its receipt.
                decision = {**route, 'execution_arguments': deepcopy(request['arguments']),
                            'domain': domain, 'depends_on': dependencies, 'prepared_command': None}
            else:
                decision = self.prepare_product_action(value, request)
                if route['kind'] == 'tool_execution':
                    decision['depends_on'] = dependencies
            output.append(decision)
            if _updates_domain_context(effective['name'], effective['arguments']):
                previous[domain] = request['invocation_id']
        return output
