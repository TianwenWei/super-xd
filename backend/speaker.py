"""Physical speaker metadata and the simulator's supported control regions."""
from copy import deepcopy
from backend.settings import DEFAULT_SPEAKER_SEAT
from backend.prompt_config import config_text

SPEAKER_NAMES = {seat: config_text('speech/vehicle.toml', 'speaker.' + seat)
                 for seat in ('driver', 'front_passenger', 'rear_left', 'rear_right')}
SPEAKER_TARGETS = {
    'driver': {'seat': 'driver', 'window': 'driver', 'door': 'driver',
               'temperature_zone': 'driver', 'interior_light_zone': 'front', 'mirror': 'left'},
    'front_passenger': {'seat': 'front_passenger', 'window': 'front_passenger', 'door': 'front_passenger',
                        'temperature_zone': 'front_passenger', 'interior_light_zone': 'front', 'mirror': 'right'},
    'rear_left': {'seat': 'rear_left', 'window': 'rear_left', 'door': 'rear_left',
                  'temperature_zone': 'rear', 'interior_light_zone': 'rear', 'mirror': 'left'},
    'rear_right': {'seat': 'rear_right', 'window': 'rear_right', 'door': 'rear_right',
                   'temperature_zone': 'rear', 'interior_light_zone': 'rear', 'mirror': 'right'},
}


def speaker_context(seat):
    if not isinstance(seat, str) or seat not in SPEAKER_TARGETS:
        raise ValueError('音区必须为driver、front_passenger、rear_left或rear_right。')
    from backend.catalog import TOOLS, HOT_TOOL_NAMES
    targets = deepcopy(SPEAKER_TARGETS[seat])
    controls = {}
    for name, spec in TOOLS.items():
        if spec.state_mapping.get('operation') == 'read':
            continue
        arguments = {}
        for field, schema in spec.parameters['properties'].items():
            candidates = ([targets['temperature_zone'], targets['interior_light_zone']] if field == 'zone'
                          else [targets['mirror']] if field == 'side'
                          else [targets['interior_light_zone']] if field == 'target'
                          else [targets[field]] if field in targets else [])
            for value in candidates:
                if value in schema.get('enum', []):
                    arguments[field] = value
                    break
        if arguments:
            controls[name] = {'tool': name if name in HOT_TOOL_NAMES else 'delegate_vehicle_control',
                              'skill_id': spec.skill_id, 'arguments': arguments}
    return {'seat': seat, 'audio_zone': seat, 'display_name': SPEAKER_NAMES[seat],
            'default_targets': targets, 'control_defaults': controls}


speaker_context(DEFAULT_SPEAKER_SEAT)
