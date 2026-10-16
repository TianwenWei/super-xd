"""Read-only foreground search using the existing Tavily transport."""
import os

from backend.models import TavilyClient


WEB_TOOLS = [{
    'type': 'function',
    'function': {
        'name': 'web_search',
        'description': '用Tavily直接搜索网页，在本轮回答简单实时事实或用户明确要求的网页搜索。返回搜索摘要、来源URL和查询时间，不代表已读取或核实网页全文。复杂比较、多步调研和指定来源深入核验交给delegate_task；天气用get_weather，地点用search_places。',
        'parameters': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'minLength': 1, 'maxLength': 800},
                'max_results': {'type': 'integer', 'minimum': 1, 'maximum': 5},
                'include_domains': {
                    'type': 'array', 'minItems': 1, 'maxItems': 5, 'uniqueItems': True,
                    'items': {'type': 'string', 'minLength': 3, 'maxLength': 253},
                    'description': '仅在用户明确指定来源且确知域名时填写，不猜测域名。',
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
                'notice': '以下为网页搜索摘要，未读取或核实页面全文。'}

    async def aclose(self):
        await self.tavily.aclose()
