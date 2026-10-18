"""Explicit media providers; only configured content can become player commands."""
from super_xd.nlp.contracts.providers import ProviderError
from super_xd.nlp.media_diagnostics import provider_diagnostic
from urllib.parse import urlparse
import httpx
import math
import time

from super_xd.nlp.contracts.media import CockpitError, media_item










class TencentMusicGateway:
    """An operator-supplied gateway contract, not an invented public Tencent API."""
    def __init__(self, *, base_url='', api_key='', client=None, timeout_seconds=12, require_api_key=True):
        self.base_url, self.api_key = base_url.rstrip('/'), api_key
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or type(require_api_key) is not bool:
            raise CockpitError('invalid_configuration', 'Media gateway timeout and authorization policy are invalid')
        self.timeout_seconds, self.require_api_key = timeout_seconds, require_api_key
        if self.base_url:
            parsed = urlparse(self.base_url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                raise CockpitError('invalid_configuration', 'Media gateway base URL is invalid')
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)
        self.owns_client = client is None

    def info(self):
        return {'provider': 'tencent_gateway', 'configured': bool(self.base_url) and (bool(self.api_key) or not self.require_api_key),
                'mode': 'enterprise_gateway', 'kinds': ['music'], 'execution_location': 'cloud', 'timeout_seconds': self.timeout_seconds}

    async def _request(self, endpoint, payload):
        provider = self.info()['provider']
        if not self.info()['configured']:
            raise ProviderError('not_configured', provider + ' media gateway is not configured', provider=provider)
        headers = {'Authorization': 'Bearer ' + self.api_key} if self.api_key else {}
        trace = {'provider': provider, 'request': {'method': 'POST', 'endpoint': endpoint, 'payload': payload,
                  'headers': headers, 'timeout_seconds': self.timeout_seconds}}
        started = time.perf_counter()
        try:
            response = await self.client.post(self.base_url + endpoint, json=payload, headers=headers, timeout=self.timeout_seconds)
            try:
                raw = response.json()
            except ValueError:
                raw = response.text
            trace['response'] = {'status_code': response.status_code, 'body': raw}
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            code = 'auth' if status in (401,403) else 'rate_limit' if status == 429 else 'unavailable' if status >= 500 else 'invalid_response'
            trace['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 1)
            raise ProviderError(code, provider + ' media gateway request failed', provider=provider,
                retryable=code in ('rate_limit','unavailable'), details=provider_diagnostic(trace,(self.api_key,))) from None
        except httpx.TimeoutException:
            trace['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 1)
            raise ProviderError('timeout', provider + ' media gateway timed out', provider=provider, retryable=True,
                details=provider_diagnostic(trace,(self.api_key,))) from None
        except httpx.RequestError:
            trace['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 1)
            raise ProviderError('unavailable', provider + ' media gateway is unavailable', provider=provider, retryable=True,
                details=provider_diagnostic(trace,(self.api_key,))) from None
        trace['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 1)
        trace = provider_diagnostic(trace,(self.api_key,))
        if not isinstance(raw, dict) or raw.get('ok') is not True:
            raise ProviderError('invalid_response', provider + ' media gateway returned invalid content', provider=provider, details=trace)
        return raw, trace

    async def search(self, *, kind, query, max_results=5):
        if kind not in self.info()['kinds'] or not isinstance(query,str) or not query.strip() or type(max_results) is not int or not 1 <= max_results <= 10:
            raise CockpitError('invalid_query', 'Media gateway search requires a supported kind, query and limit')
        result, trace = await self._request('/v1/media/search', {'kind': kind, 'query': query, 'max_results': max_results})
        try:
            if not isinstance(result.get('items'), list):
                raise CockpitError('invalid_content', 'Gateway media search items are invalid')
            items = [media_item(item) for item in result['items'][:max_results]]
            if any(item['kind'] != kind for item in items):
                raise CockpitError('invalid_content', 'Gateway returned a different content kind')
        except CockpitError:
            raise ProviderError('invalid_response', self.info()['provider'] + ' media search response is invalid',
                                provider=self.info()['provider'], details=trace) from None
        for item in items: item['provider'] = self.info()['provider']
        return {'ok': True, 'kind': 'media', 'provider': self.info()['provider'], 'items': items, 'query': query, '_provider_trace': trace}

    async def resolve(self, content_id):
        result, trace = await self._request('/v1/media/resolve', {'content_id': content_id})
        try:
            item = media_item(result.get('item'), playable=True)
            if item['content_id'] != content_id or item['kind'] not in self.info()['kinds']:
                raise CockpitError('invalid_content', 'Gateway resolved a different content ID or kind')
        except CockpitError:
            raise ProviderError('invalid_response', self.info()['provider'] + ' media resolve response is invalid',
                                provider=self.info()['provider'], details=trace) from None
        item.update(provider=self.info()['provider'], _provider_trace=trace)
        return item

    async def aclose(self):
        if self.owns_client:
            await self.client.aclose()


class DeviceMediaCatalog:
    """Routing metadata only; a global provider never owns session/device state."""
    def info(self):
        return {'provider': 'device_catalog', 'execution_location': 'device', 'configured': True, 'device_required': True}

    async def search(self, **arguments):
        raise CockpitError('context_required', 'Device media queries require a bound session gateway')

    async def resolve(self, content_id):
        raise CockpitError('context_required', 'Device media resolution requires a bound session gateway')

    async def aclose(self):
        pass
