"""Manual read-tool schemas, independent of retrieval implementations."""


MANUAL_TOOLS = [{
    'type': 'function', 'function': {
        'name': 'search_manual',
        'description': '',
        'parameters': {'type': 'object', 'properties': {
            'query': {'type': 'string', 'minLength': 1, 'maxLength': 800,
                      'description': ''},
            'max_results': {'type': 'integer', 'minimum': 1, 'maximum': 5}},
            'required': ['query'], 'additionalProperties': False}}}]

MANUAL_BY_NAME = {tool['function']['name']: tool for tool in MANUAL_TOOLS}
