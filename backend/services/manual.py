"""Manual resources are independent content providers, separate from embeddings."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

import httpx

from backend.models import utcnow
from backend.settings import ROOT
from .config import api_key, resolved, validate_url
from .contracts import ManualProvider, ProviderError, redact
from .reads import _failure, _http_url


class LocalRAGManualProvider:
    """The local vector index is one resource provider, not the CP interface itself."""
    provider = 'local_rag'

    def __init__(self, client=None, *, path=None, embeddings=None):
        from backend.manual_search import ManualSearchClient
        self.client = client if client is not None else ManualSearchClient(path, embeddings)
        self._owns_client, self._closed = client is None, False

    def info(self):
        return {**self.client.info(), 'provider': self.provider, 'execution_location': 'cloud',
                'retrieval': 'local_vector_index', 'capabilities': {'search': True, 'files': True, 'page_images': True},
                'embedding': self.client.embedding_info()}

    async def prepare(self):
        await self.client.prepare()
        return self.info()

    async def search_manual(self, query, max_results=5):
        status = self.client.embedding_info()
        if not status.get('configured'):
            raise ProviderError('not_configured', 'Local manual embedding provider is not configured', provider=self.provider,
                                details={'missing_fields': status.get('missing_fields', [])})
        try:
            return await self.client.search_manual(query, max_results)
        except ProviderError:
            raise
        except ValueError as exc:
            raise ProviderError('invalid_request', str(exc), provider=self.provider) from None
        except Exception as exc:
            error = _failure(exc, self.provider)
            if self.client.info().get('status') == 'unavailable':
                raise ProviderError('unavailable', str(error), provider=self.provider, details=error.details) from None
            raise error from None

    async def with_page_images(self, result):
        return await self.client.with_page_images(result)

    async def document(self):
        return await self.client.document()

    async def page_image(self, manual_id, page):
        return await self.client.page_image(manual_id, page)

    async def aclose(self):
        if not self._closed:
            self._closed = True
            if self._owns_client:
                await self.client.aclose()


def _identifier(value):
    return type(value) is int and value > 0 or isinstance(value, str) and bool(value.strip()) and len(value) <= 200


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _manual_result(actual, query, max_results, provider):
    if not isinstance(actual, dict) or actual.get('ok') is not True or not isinstance(actual.get('hits'), list):
        raise ProviderError('invalid_response', 'Manual CP must return an ok object and hits array', provider=provider)
    if len(actual['hits']) > max_results or 'query' in actual and actual['query'] != query:
        raise ProviderError('invalid_response', 'Manual CP result does not match the requested query/limit', provider=provider)
    result = deepcopy(actual)
    manual = result.get('manual')
    if result['hits'] or manual is not None:
        if (not isinstance(manual, dict) or not _identifier(manual.get('id')) or not _text(manual.get('title'))
                or type(manual.get('pages')) is not int or manual['pages'] < 1):
            raise ProviderError('invalid_response', 'Manual metadata must declare real identity, title and page count', provider=provider)
        for field in ('pdf_url', 'source_url'):
            if field in manual:
                if manual[field] is None:
                    manual.pop(field)
                elif not _http_url(manual[field]):
                    raise ProviderError('invalid_response', 'Remote manual links must be real HTTP(S) sources', provider=provider)
    sources = result.get('sources', [])
    if not isinstance(sources, list):
        raise ProviderError('invalid_response', 'Manual sources must be an array', provider=provider)
    source_ids = {}
    for source in sources:
        if (not isinstance(source, dict) or not _identifier(source.get('id')) or source['id'] in source_ids
                or not _text(source.get('title')) or not _http_url(source.get('url'))):
            raise ProviderError('invalid_response', 'Manual source identity/title/URL is invalid', provider=provider)
        source_ids[source['id']] = source['url']
    chunks = set()
    for hit in result['hits']:
        if (not isinstance(hit, dict) or not _identifier(hit.get('chunk_id')) or hit['chunk_id'] in chunks
                or type(hit.get('page')) is not int or not 1 <= hit['page'] <= manual['pages']
                or not _text(hit.get('section')) or not _text(hit.get('text'))):
            raise ProviderError('invalid_response', 'Manual excerpts require unique chunk, valid page, section and actual text', provider=provider)
        chunks.add(hit['chunk_id'])
        if 'manual_id' in hit and hit['manual_id'] != manual['id']:
            raise ProviderError('invalid_response', 'Manual excerpt belongs to a different declared document', provider=provider)
        if 'score' in hit and (type(hit['score']) not in (int, float) or not math.isfinite(hit['score'])):
            raise ProviderError('invalid_response', 'Manual ranking score must be a finite number', provider=provider)
        if 'source_id' in hit and (not _identifier(hit['source_id']) or hit['source_id'] not in source_ids):
            raise ProviderError('invalid_response', 'Manual excerpt references an unknown source', provider=provider)
        if 'source_url' in hit and not _http_url(hit['source_url']):
            raise ProviderError('invalid_response', 'Manual excerpt source URL is invalid', provider=provider)
        source = hit.get('source_url') or source_ids.get(hit.get('source_id')) or manual.get('pdf_url') or manual.get('source_url')
        if not _http_url(source):
            raise ProviderError('invalid_response', 'Every manual excerpt requires an actual source URL', provider=provider)
        hit['source_url'] = source
        images = hit.get('images', [])
        if not isinstance(images, list):
            raise ProviderError('invalid_response', 'Manual images must be an array', provider=provider)
        for image in images:
            if (not isinstance(image, dict) or not _http_url(image.get('url'))
                    or type(image.get('page')) is not int or image['page'] != hit['page']
                    or 'caption' in image and not isinstance(image['caption'], str)):
                raise ProviderError('invalid_response', 'Manual image must be a provider URL for the actual excerpt page', provider=provider)
        hit['images'] = images
    if 'notice' in result and not isinstance(result['notice'], str):
        raise ProviderError('invalid_response', 'Manual notice must be text', provider=provider)
    return {**result, 'kind': 'manual', 'provider': provider, 'query': query,
            'evidence': 'manual_text' if result['hits'] else 'no_manual_matches',
            'queried_at': result.get('queried_at', utcnow())}


class RemoteManualProvider:
    """Independent manual CP; no local index, vector model or PDF rendering required."""
    provider = 'manual_gateway'

    def __init__(self, profile, *, client=None):
        self.profile = {'key_env': 'MANUAL_API_KEY', **deepcopy(profile)}
        self.base_url = validate_url(resolved(profile, 'base_url'))
        self.key = api_key(self.profile)
        self.client = client if client is not None else httpx.AsyncClient(timeout=profile.get('timeout_seconds', 12), follow_redirects=False)
        self._owns_client, self._closed = client is None, False
        self._observed = False

    def info(self):
        configured = bool(self.base_url) and (not self.profile.get('require_api_key', True) or bool(self.key))
        return {'provider': self.provider, 'configured': configured, 'execution_location': 'cloud',
                'ready': self._observed, 'status': 'ready' if self._observed else 'not_verified' if configured else 'not_configured',
                'availability': 'query_succeeded' if self._observed else 'not_verified',
                'capabilities': {'search': True, 'files': False, 'page_images': 'provider_supplied'},
                'missing_fields': ([] if self.base_url else ['base_url']) + (
                    [self.profile['key_env']] if self.profile.get('require_api_key', True) and not self.key else [])}

    async def prepare(self):
        # Configuration does not prove remote availability. No implicit CP query.
        return self.info()

    async def search_manual(self, query, max_results=5):
        if self._closed:
            raise ProviderError('unavailable', 'Manual CP provider is closed', provider=self.provider)
        if not _text(query) or len(query) > 800 or type(max_results) is not int or not 1 <= max_results <= 5:
            raise ProviderError('invalid_request', 'Manual query must be 1–800 characters and request 1–5 excerpts', provider=self.provider)
        if not self.info()['configured']:
            raise ProviderError('not_configured', 'Independent manual CP endpoint or API key is missing', provider=self.provider,
                                details={'missing_fields': self.info()['missing_fields']})
        payload = {'query': query, 'max_results': max_results}
        try:
            async with self.client.stream('POST', self.base_url + '/v1/manual/search', json=payload,
                headers={'Authorization': 'Bearer ' + self.key} if self.key else {}) as response:
                response.raise_for_status()
                body = bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part)
                    if len(body) > 4 * 1024 * 1024:
                        raise ProviderError('invalid_response', 'Manual CP response exceeds the size limit', provider=self.provider)
                actual = json.loads(body)
            safe_actual = redact(actual, (self.key,))
            result = _manual_result(safe_actual, query, max_results, self.provider)
            result['_provider_trace'] = {'request': payload, 'response': safe_actual}
            self._observed = True
            return result
        except Exception as exc:
            raise _failure(exc, self.provider, (self.key,)) from None

    async def with_page_images(self, result):
        # Only actual CP image URLs from search_manual can be returned here.
        return deepcopy(result)

    async def document(self):
        raise ProviderError('unsupported_capability', 'This manual CP does not expose a local PDF file capability', provider=self.provider)

    async def page_image(self, manual_id, page):
        raise ProviderError('unsupported_capability', 'This manual CP does not expose local page rendering', provider=self.provider)

    async def aclose(self):
        if not self._closed:
            self._closed = True
            if self._owns_client:
                await self.client.aclose()


def build_manual(profile, *, embeddings=None):
    provider = profile.get('provider', 'local_rag')
    if provider == 'local_rag':
        path = resolved(profile, 'index_path') or None
        if path:
            path = Path(path)
            path = path if path.is_absolute() else ROOT / path
        return LocalRAGManualProvider(path=path, embeddings=embeddings)
    if provider == 'http_gateway':
        return RemoteManualProvider(profile)
    raise ValueError('Unsupported manual provider: ' + str(provider))
