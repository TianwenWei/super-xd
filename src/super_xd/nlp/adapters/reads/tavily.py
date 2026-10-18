"""Tavily HTTP adapter; search does not depend on the model transport."""
import os
import httpx
from super_xd.nlp.adapters.common import sanitized_error, utcnow
from super_xd.nlp.textutil import is_domain


class TavilyClient:
    def __init__(self, client=None, *, key_env='TAVILY_API_KEY', timeout_seconds=35):
        self.key_env = key_env
        self.client = client or httpx.AsyncClient(base_url='https://api.tavily.com/', timeout=timeout_seconds)

    async def _request(self, endpoint, body):
        key = os.getenv(self.key_env)
        if not key:
            raise RuntimeError('TAVILY_API_KEY 未配置，无法查询实时信息。')
        at = utcnow()
        try:
            response = await self.client.post(endpoint, headers={'Authorization': f'Bearer {key}'}, json=body)
            response.raise_for_status()
            data = response.json()
            data['queried_at'] = at
            return data
        except (httpx.HTTPError, ValueError) as exc:
            error = RuntimeError(f'Tavily {endpoint} 失败：{sanitized_error(exc, (key,))}')
            error.status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            error.provider_code = 'timeout' if isinstance(exc, httpx.TimeoutException) else 'unavailable' if isinstance(exc, httpx.TransportError) else None
            error.full_diagnostic = str(exc).replace(key, '[redacted]')
            raise error from None

    async def search(self, query, max_results=5, *, include_domains=None):
        body = {'query': query, 'max_results': max_results, 'search_depth': 'basic',
                'include_answer': False, 'include_raw_content': False}
        if include_domains is None:
            return await self._request('search', body)
        invalid = 'include_domains 必须是1至10个有效域名组成的列表，不含协议、路径、端口或空元素。'
        if not isinstance(include_domains, list) or not 1 <= len(include_domains) <= 10:
            raise ValueError(invalid)
        domains = []
        for domain in include_domains:
            if not is_domain(domain):
                raise ValueError(invalid)
            domains.append(domain.lower())
        body.update(include_domains=domains, include_domains_mode='restrict')
        result = await self._request('search', body)
        # Records the actual request restriction, not a claim that each
        # returned page was semantically verified or satisfies the goal.
        result['request_filters'] = {'include_domains': domains, 'include_domains_mode': 'restrict'}
        return result

    async def extract(self, urls):
        return await self._request('extract', {'urls': urls, 'extract_depth': 'basic', 'format': 'text'})

    async def aclose(self):
        await self.client.aclose()
