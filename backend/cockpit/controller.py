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


async def prepare_media(services, action, arguments, state, choices, *, trusted_selection=False):
    expected = deepcopy(state)
    payload = {}
    provider_trace = None
    known_content = {item['content_id']: item for item in state.get('queue', [])}
    known_content.update(choices)
    if action in ('play', 'next', 'previous'):
        identifier = arguments.get('content_id')
        if action != 'play':
            queue = state.get('queue', [])
            identifiers = [item['content_id'] for item in queue]
            if state.get('content_id') not in identifiers:
                raise CockpitError('queue_boundary', 'Current content has no known queue')
            index = identifiers.index(state['content_id']) + (1 if action == 'next' else -1)
            if not 0 <= index < len(identifiers):
                raise CockpitError('queue_boundary', 'No neighboring queue item')
            identifier = identifiers[index]
        if not identifier or not trusted_selection and identifier not in known_content and identifier != state.get('content_id'):
            raise CockpitError('unknown_content', 'Content ID was not found in this session')
        if (services.media.content_provider_info(identifier) if hasattr(services.media,'content_provider_info') else services.media.info()).get('execution_location') == 'device':
            if identifier not in known_content:
                raise CockpitError('unknown_content', 'Device content must first be resolved by this session')
            item = media_item(known_content[identifier])
            if hasattr(services.media,'device_content_id'):
                payload['device_content_id'] = services.media.device_content_id(identifier)
        else:
            resolved = await services.media.resolve(identifier)
            provider_trace = deepcopy(resolved.get('_provider_trace'))
            item = media_item(resolved, playable=True)
        payload.update(item)
        expected.update({key: item.get(key) for key in ('content_id', 'kind', 'title', 'provider')},
                        status='playing', desired_status='playing', position_seconds=0, duration_seconds=item.get('duration_seconds'))
        if action == 'play' and choices and identifier in choices:
            expected['queue'] = [deepcopy(value) for value in choices.values() if value['kind'] == item['kind']]
    elif action in ('pause', 'resume', 'stop', 'seek', 'volume', 'exit'):
        if action not in ('volume', 'exit') and not state.get('content_id'):
            raise CockpitError('no_content', 'Player has no content')
        if action == 'seek':
            position = arguments.get('position_seconds')
            if not finite(position) or position < 0 or finite(state.get('duration_seconds')) and position > state['duration_seconds']:
                raise CockpitError('invalid_action', 'Seek position is out of range')
            payload['position_seconds'] = position
            expected['position_seconds'] = position
        elif action == 'volume':
            volume = arguments.get('volume')
            if not finite(volume) or not 0 <= volume <= 100:
                raise CockpitError('invalid_action', 'Media volume is out of range')
            payload['volume'] = volume
            expected['volume'] = volume
        elif action == 'exit':
            expected.update(status='closed', desired_status='closed', content_id=None, kind=None, title='', position_seconds=0, duration_seconds=None, provider=None)
        else:
            status = {'pause': 'paused', 'resume': 'playing', 'stop': 'stopped'}[action]
            expected.update(status=status, desired_status=status)
            if action == 'stop':
                expected['position_seconds'] = 0
    else:
        raise CockpitError('invalid_action', 'Unknown media action')
    return {'domain': 'media', 'action': action, 'arguments': payload, 'expected': expected, 'provider_trace': provider_trace}


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
