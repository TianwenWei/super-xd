"""Web read-tool schemas, independent of retrieval implementations."""


WEB_TOOLS = [{
    'type': 'function',
    'function': {
        'name': 'web_search',
        'description': '',
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'minLength': 1, 'maxLength': 800},
                'max_results': {'type': 'integer', 'minimum': 1, 'maximum': 5},
                'include_domains': {
                    'type': 'array', 'minItems': 1, 'maxItems': 5, 'uniqueItems': True,
                    'items': {'type': 'string', 'minLength': 3, 'maxLength': 253},
                    'description': '',
                },
            },
            'required': ['query'],
            'additionalProperties': False,
        },
    },
}]

WEB_BY_NAME = {tool['function']['name']: tool for tool in WEB_TOOLS}
