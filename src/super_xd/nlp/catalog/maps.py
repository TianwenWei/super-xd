"""Place/weather/navigation schemas, independent of provider transports."""


def tool(name, description, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties, 'required': required,
                       'additionalProperties': False}}}


MAPS_TOOLS = [
    tool('search_places', '',
         {'query': {'type': 'string', 'minLength': 1, 'maxLength': 300},
          'area': {'type': 'string', 'minLength': 1, 'maxLength': 200,
                   'description': ''},
          'max_results': {'type': 'integer', 'minimum': 1, 'maximum': 5}}, ['query', 'area']),
    tool('get_weather', '',
         {'location': {'type': 'string', 'minLength': 1, 'maxLength': 200},
          'date': {'type': 'string', 'pattern': r'^(today|tomorrow|day_after_tomorrow|now|\d{4}-\d{2}-\d{2})$'}}, ['location', 'date']),
    tool('start_poi_navigation', '',
         {'place_id': {'type': 'string', 'minLength': 1, 'maxLength': 200}}, ['place_id']),
]
MAPS_BY_NAME = {item['function']['name']: item for item in MAPS_TOOLS}
READ_TOOLS = {'search_places', 'get_weather'}
