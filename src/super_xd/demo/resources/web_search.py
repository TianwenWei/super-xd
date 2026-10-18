"""Read-only foreground search using the existing Tavily transport."""
from super_xd.nlp.prompts.speech import feedback_text
import os

from super_xd.nlp.adapters.reads.tavily import TavilyClient


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
