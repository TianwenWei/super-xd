"""Read-only foreground search using the existing Tavily transport."""
from backend.speech_config import feedback_text
from backend.tool_text_config import tool_text
import os

from backend.models import TavilyClient


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


class WebSearchClient:
    def __init__(self, tavily=None):
        self.tavily = tavily or TavilyClient()

    def info(self):
        return {'provider': 'Tavily', 'configured': bool(os.getenv('TAVILY_API_KEY'))}

    async def web_search(self, query, max_results=3, include_domains=None):
        result = await self.tavily.search(query, max_results, include_domains=include_domains)
        if not isinstance(result.get('results'), list):
            raise RuntimeError('Tavily search 返回的结果格式无效，未取得可用搜索结果。')
        return {**result, 'ok': True, 'kind': 'web_search', 'provider': 'Tavily',
                'query': query, 'evidence': 'search_snippets',
                'notice': feedback_text('web_search.notice')}

    async def aclose(self):
        await self.tavily.aclose()
