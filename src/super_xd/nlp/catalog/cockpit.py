"""Three stable domain tools; providers never expand the Talker's interface."""
from super_xd.nlp.catalog.actions import MEDIA_ACTIONS, NAVIGATION_ACTIONS

TOOL_KEYS = ('search_media', 'control_media', 'manage_navigation')



def tool(name, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': '',
        'parameters': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}}


COCKPIT_TOOLS = [
    tool('search_media', {'kind': {'type': 'string', 'enum': ['music', 'video', 'radio']},
                         'query': {'type': 'string', 'minLength': 1, 'maxLength': 300},
                         'max_results': {'type': 'integer', 'minimum': 1, 'maximum': 10}}, ['kind', 'query']),
    tool('control_media', {'action': {'type': 'string', 'enum': list(MEDIA_ACTIONS)},
                          'content_id': {'type': 'string', 'minLength': 1, 'maxLength': 500},
                          'position_seconds': {'type': 'number', 'minimum': 0},
                          'volume': {'type': 'number', 'minimum': 0, 'maximum': 100}}, ['action']),
    tool('manage_navigation', {'action': {'type': 'string', 'enum': list(NAVIGATION_ACTIONS)},
                              'place_ref': {'type': 'string', 'minLength': 1, 'maxLength': 300},
                              'origin_ref': {'type': 'string', 'minLength': 1, 'maxLength': 300},
                              'waypoint_ref': {'type': 'string', 'minLength': 1, 'maxLength': 300}}, ['action']),
]
COCKPIT_BY_NAME = {item['function']['name']: item for item in COCKPIT_TOOLS}
