"""Foreground FC definitions. Read-only network calls stay outside the actor loop."""
from backend.tool_text_config import tool_text


def tool(name, description, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties, 'required': required,
                       'additionalProperties': False}}}


MAPS_TOOLS = [
    tool('search_places', tool_text('search_places.description'),
         {'query': {'type': 'string', 'minLength': 1, 'maxLength': 300},
          'area': {'type': 'string', 'minLength': 1, 'maxLength': 200,
                   'description': tool_text('search_places.parameters.area.description')},
          'max_results': {'type': 'integer', 'minimum': 1, 'maximum': 5}}, ['query', 'area']),
    tool('get_weather', tool_text('get_weather.description'),
         {'location': {'type': 'string', 'minLength': 1, 'maxLength': 200},
          'date': {'type': 'string', 'pattern': r'^(today|tomorrow|day_after_tomorrow|now|\d{4}-\d{2}-\d{2})$'}}, ['location', 'date']),
    tool('start_poi_navigation', tool_text('start_poi_navigation.description'),
         {'place_id': {'type': 'string', 'minLength': 1, 'maxLength': 200}}, ['place_id']),
]
MAPS_BY_NAME = {item['function']['name']: item for item in MAPS_TOOLS}
READ_TOOLS = {'search_places', 'get_weather'}
