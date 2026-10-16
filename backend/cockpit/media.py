"""Explicit media providers; only configured content can become player commands."""
from copy import deepcopy
from backend.services.contracts import MediaCatalog, ProviderError, redact
from backend.textutil import find_urls
from urllib.parse import urlparse
import httpx
import math
import time

KINDS = ('music', 'video', 'radio')


class CockpitError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def media_item(value, *, playable=False):
    if not isinstance(value, dict) or value.get('kind') not in KINDS:
        raise CockpitError('invalid_content', 'Media item kind is invalid')
    identifier, title = value.get('content_id'), value.get('title')
    if not isinstance(identifier, str) or not identifier or len(identifier) > 500:
        raise CockpitError('invalid_content', 'Media content_id is invalid')
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise CockpitError('invalid_content', 'Media title is invalid')
    for field in ('artist', 'album', 'provider', 'mime_type'):
        if field in value and (not isinstance(value[field], str) or len(value[field]) > 500):
            raise CockpitError('invalid_content', 'Media descriptive metadata is invalid')
    duration = value.get('duration_seconds')
    if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0):
        raise CockpitError('invalid_content', 'Media duration is invalid')
    result = {key: deepcopy(value[key]) for key in ('content_id', 'kind', 'title', 'artist', 'album', 'provider', 'mime_type', 'duration_seconds') if key in value}
    if playable:
        url = value.get('source_url')
        parsed = urlparse(url) if isinstance(url, str) else None
        local = (isinstance(url, str) and url.startswith('/api/media/') and not url.startswith('//')
                 and not any(part in ('.', '..') for part in parsed.path.split('/')) and '\\' not in url)
        remote = parsed and parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password
        if not (local or remote):
            raise CockpitError('invalid_source', 'Media source must be a configured HTTP URL or local media endpoint')
        result['source_url'] = url
    return result


class LocalMediaCatalog:
    """Development catalog is opt-in and never stands in for Tencent content."""
    def __init__(self, entries=()):
        self.entries = {}
        for value in entries:
            item = media_item(value, playable=True)
            if item['content_id'] in self.entries:
                raise CockpitError('duplicate_content', 'Duplicate media content_id')
            item.setdefault('provider', 'local_catalog')
            self.entries[item['content_id']] = item

    def info(self):
        return {'provider': 'local_catalog', 'configured': bool(self.entries), 'mode': 'explicit_development', 'count': len(self.entries), 'execution_location': 'cloud'}

    async def search(self, *, kind, query, max_results=5):
        if kind not in KINDS or not isinstance(query, str) or not query.strip():
            raise CockpitError('invalid_query', 'Media search requires kind and query')
        if type(max_results) is not int or not 1 <= max_results <= 10:
            raise CockpitError('invalid_query', 'Media search result limit is invalid')
        words = query.casefold().split()
        # This is catalog retrieval over a structured query, never intent routing.
        ranked = []
        for item in self.entries.values():
            if item['kind'] != kind:
                continue
            searchable = ' '.join(str(item.get(key, '')) for key in ('title', 'artist', 'album')).casefold()
            score = sum(word in searchable for word in words)
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda row: (-row[0], row[1]['content_id']))
        return {'ok': True, 'kind': 'media', 'provider': 'local_catalog', 'items': [media_item(item) for _, item in ranked[:max_results]], 'query': query}

    async def resolve(self, content_id):
        if content_id not in self.entries:
            raise CockpitError('unknown_content', 'Content is not in the configured catalog')
        return deepcopy(self.entries[content_id])

    async def aclose(self):
        pass


def provider_diagnostic(value, secrets=()):
    """Playback URLs are capabilities, including URLs echoed inside raw errors."""
    safe = redact(value, secrets)
    if isinstance(safe, dict):
        return {key: ('[redacted playback capability]' if str(key).lower() in ('source_url', 'url', 'playback_url', 'stream_url', 'download_url')
                      else provider_diagnostic(item, secrets)) for key, item in safe.items()}
    if isinstance(safe, list):
        return [provider_diagnostic(item, secrets) for item in safe]
    if isinstance(safe, str):
        for url in find_urls(safe):
            safe = safe.replace(url, '[redacted playback capability]')
    return safe


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
