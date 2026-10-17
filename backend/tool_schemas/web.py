"""Web read-tool schemas, independent of retrieval implementations."""
from backend.tool_text_config import tool_text


WEB_TOOLS = [{
    'type': 'function',
    'function': {
        'name': 'web_search',
        'description': tool_text('web_search.description'),
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'minLength': 1, 'maxLength': 800},
                'max_results': {'type': 'integer', 'minimum': 1, 'maximum': 5},
                'include_domains': {
                    'type': 'array', 'minItems': 1, 'maxItems': 5, 'uniqueItems': True,
                    'items': {'type': 'string', 'minLength': 3, 'maxLength': 253},
                    'description': tool_text('web_search.parameters.include_domains.description'),
                },
            },
            'required': ['query'],
            'additionalProperties': False,
        },
    },
}]

WEB_BY_NAME = {tool['function']['name']: tool for tool in WEB_TOOLS}
