"""Pure command preparation/receipt validation; actor remains the state writer."""
from copy import deepcopy
import math
from .media import CockpitError, media_item


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def scoped_place(place, provider=None):
    if not isinstance(place, dict) or not isinstance(place.get('name'), str) or not place['name'] or len(place['name']) > 500:
        raise CockpitError('unknown_place', 'Place name is invalid')
    location = place.get('location')
    if not isinstance(location, dict) or not finite(location.get('latitude')) or not finite(location.get('longitude')) or not -90 <= location['latitude'] <= 90 or not -180 <= location['longitude'] <= 180:
        raise CockpitError('unknown_place', 'Place coordinates are invalid')
    result = deepcopy(place)
    provider = result.get('provider') or provider or 'unknown'
    provider = {'Google Maps': 'google', 'Google Places': 'google', '高德地图': 'amap', '百度地图': 'baidu'}.get(provider, provider)
    identifier = result.get('provider_id') or result.get('place_id')
    if not isinstance(identifier, str) or not identifier or not isinstance(provider, str) or not provider:
        raise CockpitError('unknown_place', 'Place must retain provider and provider ID')
    reference = provider + ':' + identifier
    if result.get('place_ref', reference) != reference:
        raise CockpitError('unknown_place', 'Place reference does not match its provider scope')
    result.update(provider=provider, provider_id=identifier, place_ref=reference)
    result.setdefault('coordinate_system', 'wgs84' if provider == 'google' else 'unknown')
    return result


def observed_media_state(previous, result):
    incoming = result.get('state')
    version = result.get('state_version', incoming.get('version') if isinstance(incoming, dict) else None)
    if not isinstance(incoming, dict) or type(version) is not int or version < max(0, previous['version']):
        raise CockpitError('stale_receipt', 'Invalid device status version')
    if incoming.get('status') not in ('playing', 'paused', 'stopped', 'closed'):
        raise CockpitError('device_failed', 'Invalid device playback status')
    state = deepcopy(previous)
    if incoming['status'] != 'closed':
        if not isinstance(incoming.get('content_id'), str) or not incoming['content_id'] or incoming.get('kind') not in ('music', 'video', 'radio'):
            raise CockpitError('device_failed', 'Invalid observed device content')
    if incoming.get('desired_status', incoming['status']) not in ('playing', 'paused', 'stopped', 'closed'):
        raise CockpitError('device_failed', 'Invalid observed desired status')
    if 'title' in incoming and (not isinstance(incoming['title'], str) or len(incoming['title']) > 500):
        raise CockpitError('device_failed', 'Invalid observed title')
    for key in ('position_seconds', 'duration_seconds', 'volume'):
        value = incoming.get(key)
        if value is not None and (not finite(value) or value < 0 or key == 'volume' and value > 100):
            raise CockpitError('device_failed', 'Invalid observed playback value')
    for key in ('content_id', 'kind', 'title', 'status', 'desired_status', 'position_seconds', 'duration_seconds', 'volume', 'focus_reason'):
        if key in incoming:
            state[key] = deepcopy(incoming[key])
    if incoming.get('content_id') != previous.get('content_id'):
        state['provider'] = None
    if 'provider' in incoming:
        provider = incoming['provider']
        if provider is not None and (not isinstance(provider, str) or not provider or len(provider) > 500):
            raise CockpitError('device_failed', 'Invalid observed content provider')
        state['provider'] = provider
    if 'queue' in incoming:
        queue = incoming['queue']
        if not isinstance(queue, list) or len(queue) > 100:
            raise CockpitError('device_failed', 'Invalid observed media queue')
        queue = [media_item(item) for item in queue]
        if any(not item.get('provider') for item in queue) or len({item['content_id'] for item in queue}) != len(queue):
            raise CockpitError('device_failed', 'Observed queue requires unique sourced content')
        state['queue'] = queue
    if incoming['status'] == 'closed':
        state.update(content_id=None, kind=None, title='', provider=None, desired_status='closed',
                     position_seconds=0, duration_seconds=None)
    state['version'] = version
    return state


def _media_selection(action, arguments, state, known, *, trusted_selection=False):
    identifier = arguments.get('content_id')
    if action != 'play':
        identifiers = [item['content_id'] for item in state.get('queue', [])]
        if identifiers.count(state.get('content_id')) != 1:
            raise CockpitError('queue_boundary', 'Current content has no unambiguous queue position')
        index = identifiers.index(state['content_id']) + (1 if action == 'next' else -1)
        if not 0 <= index < len(identifiers):
            raise CockpitError('queue_boundary', 'No neighboring queue item')
        identifier = identifiers[index]
    if not identifier or not trusted_selection and identifier not in known and identifier != state.get('content_id'):
        raise CockpitError('unknown_content', 'Content ID was not found in supplied catalog or player state')
    return identifier


def prepare_media_action(action, arguments, state, choices, *, resolved_content=None, require_provider=True):
    """Prepare content references and expected state without resolving a URL."""
    if action not in {'play', 'next', 'previous', 'pause', 'resume', 'stop', 'exit', 'seek', 'volume'}:
        raise CockpitError('invalid_action', 'Unknown media action')
    expected, payload = deepcopy(state), {}
    known = {item['content_id']: item for item in state.get('queue', [])}
    known.update(choices)
    if resolved_content is not None:
        known[resolved_content['content_id']] = resolved_content
    if action in {'play', 'next', 'previous'}:
        identifier = _media_selection(action, arguments, state, known)
        if identifier not in known:
            known[identifier] = state
        item = media_item(known[identifier])
        if require_provider and not item.get('provider'):
            raise CockpitError('unknown_content', 'Content must retain its catalog provider')
        payload.update(item)
        expected.update({key: item.get(key) for key in ('content_id', 'kind', 'title', 'provider')},
                        status='playing', desired_status='playing', position_seconds=0,
                        duration_seconds=item.get('duration_seconds'))
        if action == 'play' and identifier in choices:
            expected['queue'] = [deepcopy(item) for item in choices.values() if item['kind'] == expected['kind']]
    else:
        if action not in {'volume', 'exit'} and not state.get('content_id'):
            raise CockpitError('no_content', 'Player has no content')
        if action == 'seek':
            position = arguments.get('position_seconds')
            if not finite(position) or position < 0 or finite(state.get('duration_seconds')) and position > state['duration_seconds']:
                raise CockpitError('invalid_action', 'Seek position is out of range')
            payload['position_seconds'] = expected['position_seconds'] = position
        elif action == 'volume':
            volume = arguments.get('volume')
            if not finite(volume) or not 0 <= volume <= 100:
                raise CockpitError('invalid_action', 'Media volume is out of range')
            payload['volume'] = expected['volume'] = volume
        elif action == 'exit':
            expected.update(status='closed', desired_status='closed', content_id=None, kind=None, title='',
                            position_seconds=0, duration_seconds=None, provider=None)
        else:
            status = {'pause': 'paused', 'resume': 'playing', 'stop': 'stopped'}[action]
            expected.update(status=status, desired_status=status)
            if action == 'stop':
                expected['position_seconds'] = 0
    return {'domain': 'media', 'action': action, 'arguments': payload, 'expected': expected,
            'requires_content_resolution': action in {'play', 'next', 'previous'}}


async def prepare_media(services, action, arguments, state, choices, *, trusted_selection=False):
    """Resolve a sourced catalog reference, then use the shared pure state rules."""
    resolved, provider_trace, device_content_id = None, None, None
    if action in ('play', 'next', 'previous'):
        known = {item['content_id']: item for item in state.get('queue', [])}
        known.update(choices)
        identifier = _media_selection(action, arguments, state, known, trusted_selection=trusted_selection)
        info = services.media.content_provider_info(identifier) if hasattr(services.media, 'content_provider_info') else services.media.info()
        if info.get('execution_location') == 'device':
            if identifier not in known:
                raise CockpitError('unknown_content', 'Device content must first be resolved by this session')
            resolved = media_item(known[identifier])
            if hasattr(services.media, 'device_content_id'):
                device_content_id = services.media.device_content_id(identifier)
        else:
            value = await services.media.resolve(identifier)
            provider_trace = deepcopy(value.get('_provider_trace'))
            resolved = media_item(value, playable=True)
            if resolved['content_id'] != identifier:
                raise CockpitError('invalid_content', 'Resolved content ID does not match its catalog reference')
    command = prepare_media_action(action, arguments, state, choices, resolved_content=resolved, require_provider=False)
    command.pop('requires_content_resolution')
    if resolved is not None and 'source_url' in resolved:
        command['arguments']['source_url'] = resolved['source_url']
    if device_content_id is not None:
        command['arguments']['device_content_id'] = device_content_id
    command['provider_trace'] = provider_trace
    return command


def prepare_navigation(action, arguments, state, places):
    expected = deepcopy(state)
    if action in ('start', 'add_waypoint', 'set_origin'):
        place = deepcopy(places[arguments['place_ref']])
        if action == 'set_origin':
            expected.update(origin=place, route=None)
        elif action == 'start':
            expected.update(active=True, destination=place, waypoints=[], route=None)
        else:
            if not state.get('destination'):
                raise CockpitError('no_content', 'Navigation needs a destination first')
            if len(state['waypoints']) >= 10:
                raise CockpitError('invalid_action', 'Navigation supports at most ten waypoints')
            expected['waypoints'].append(place)
            expected['route'] = None
    elif action == 'remove_waypoint':
        reference = arguments.get('waypoint_ref')
        matches = [index for index, place in enumerate(state['waypoints']) if place['place_ref'] == reference]
        if len(matches) != 1:
            raise CockpitError('unknown_place', 'Waypoint reference is not unique in this itinerary')
        expected['waypoints'].pop(matches[0])
        expected['route'] = None
    elif action == 'cancel':
        expected.update(active=False, destination=None, waypoints=[], route=None)
    else:
        raise CockpitError('invalid_action', 'Unknown navigation action')
    return {'domain': 'navigation', 'action': action, 'arguments': {'state': deepcopy(expected)}, 'expected': expected}


def validated_receipt(command, receipt):
    expected = deepcopy(command['expected'])
    version = command['wire']['expected_state_version'] + 1
    if type(receipt.get('state_version')) is not int or receipt['state_version'] != version:
        raise CockpitError('stale_receipt', 'Receipt version does not match the command')
    state = receipt.get('state')
    if not isinstance(state, dict):
        raise CockpitError('device_failed', 'Device did not return actual state')
    if command['wire']['domain'] == 'navigation':
        for key in ('mode', 'active', 'destination', 'waypoints', 'origin'):
            if state.get(key) != expected.get(key):
                raise CockpitError('device_failed', 'Navigation receipt does not match the requested itinerary')
    else:
        if state.get('content_id') != expected.get('content_id'):
            raise CockpitError('device_failed', 'Player reported a different content ID')
        status = state.get('status')
        focus_paused = expected['desired_status'] == 'playing' and status == 'paused' and state.get('focus_reason') == 'speech'
        if status != expected['status'] and not focus_paused:
            raise CockpitError('device_failed', 'Player state does not confirm the requested action')
        for key in ('position_seconds', 'duration_seconds', 'volume'):
            value = state.get(key)
            if value is not None:
                if not finite(value) or value < 0 or key == 'volume' and value > 100:
                    raise CockpitError('device_failed', 'Player reported invalid numeric state')
                expected[key] = value
        expected['status'] = status
        expected['focus_reason'] = state.get('focus_reason') if focus_paused else None
    expected['version'] = version
    return expected
