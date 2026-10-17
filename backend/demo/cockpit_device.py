"""Independent in-memory device for contract tests and behavior evaluation."""
from __future__ import annotations

from copy import deepcopy

from backend.cockpit import initial_cockpit_state
from backend.cockpit.media import media_item


class CockpitDevice:
    """Apply command payloads to device-owned state, never NLP expected state."""

    def __init__(self, state=None):
        self.state = deepcopy(initial_cockpit_state() if state is None else state)
        self.receipts = {}

    def execute(self, command):
        key = command['request_id']
        if key in self.receipts:
            return deepcopy(self.receipts[key])
        domain, action, arguments = command['domain'], command['action'], command['arguments']
        state = deepcopy(self.state[domain])
        identity = {name: command[name] for name in
                    ('command_id', 'request_id', 'session_id', 'turn_id', 'target_device', 'kind')}
        if command['kind'] == 'query':
            receipt = {**identity, 'ok': True, 'result': {'ok': True, 'state': state, 'state_version': state['version']}}
        elif state['version'] != command['expected_state_version']:
            receipt = {**identity, 'ok': False, 'error': {'code': 'state_conflict', 'retryable': False}}
        else:
            if domain == 'navigation':
                requested = arguments.get('state')
                if not isinstance(requested, dict):
                    raise ValueError('Navigation commands require an explicit itinerary')
                for name in ('active', 'destination', 'waypoints', 'origin'):
                    state[name] = deepcopy(requested[name])
                state['route'] = None
            elif action in {'play', 'next', 'previous'}:
                if 'queue' in arguments:
                    queue = [media_item(item) for item in arguments['queue']]
                    identifiers = [item['content_id'] for item in queue]
                    if len(queue) > 100 or len(set(identifiers)) != len(identifiers) or queue and arguments['content_id'] not in identifiers:
                        raise ValueError('A player queue requires unique content and the selected item')
                    state['queue'] = queue
                for name in ('content_id', 'kind', 'title', 'provider', 'duration_seconds'):
                    state[name] = deepcopy(arguments.get(name))
                if not state['content_id']:
                    raise ValueError('A player command requires a sourced content identity')
                state.update(status='playing', desired_status='playing', position_seconds=0)
            elif action in {'pause', 'resume', 'stop'}:
                if not state.get('content_id'):
                    raise ValueError('The device has no active content')
                status = {'pause': 'paused', 'resume': 'playing', 'stop': 'stopped'}[action]
                state.update(status=status, desired_status=status)
                if action == 'stop':
                    state['position_seconds'] = 0
            elif action == 'exit':
                state.update(status='closed', desired_status='closed', content_id=None, kind=None,
                             title='', provider=None, position_seconds=0, duration_seconds=None)
            elif action == 'volume':
                state['volume'] = arguments['volume']
            elif action == 'seek':
                state['position_seconds'] = arguments['position_seconds']
            else:
                raise ValueError('Unknown local device action')
            state['version'] += 1
            self.state[domain] = state
            receipt = {**identity, 'ok': True, 'state': deepcopy(state), 'state_version': state['version']}
        self.receipts[key] = deepcopy(receipt)
        return receipt
