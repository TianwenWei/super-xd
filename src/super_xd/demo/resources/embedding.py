"""Embedding providers preserve corpus/query instructions and index identity."""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from urllib.parse import urlparse

import httpx

from super_xd.demo.resources.manual_embeddings import ManualEmbeddingClient
from super_xd.nlp.prompts.templates import load_prompt
from super_xd.nlp.contracts.providers import ProviderError, redact


def _dimensions(value):
    if type(value) is not int or not 1 <= value <= 65536:
        raise ValueError('Embedding dimensions must be between 1 and 65536')
    return value


def _normalize(vector, dimensions, provider):
    if (not isinstance(vector, list) or len(vector) != dimensions or
            any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)):
        raise ProviderError('invalid_response', 'Embedding response has invalid dimensions or numeric values', provider=provider)
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm == 0:
        raise ProviderError('invalid_response', 'Embedding vector cannot be normalized', provider=provider)
    return [value / norm for value in vector]


def _failure(exc, provider, key):
    if isinstance(exc, ProviderError):
        return exc
    status = getattr(exc, 'status_code', None)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    code = getattr(exc, 'provider_code', None)
    if code is None:
        if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
            code = 'timeout'
        elif status in (401, 403):
            code = 'auth'
        elif status == 429:
            code = 'rate_limit'
        elif isinstance(exc, (httpx.RequestError, OSError)) or isinstance(status, int) and status >= 500:
            code = 'unavailable'
        else:
            code = 'invalid_response'
    message = redact(str(exc), secrets=(key,))
    return ProviderError(code, message, provider=provider, retryable=code in {'timeout', 'rate_limit', 'unavailable'},
                         details={'status_code': status, 'exception_type': type(exc).__name__, 'diagnostic': message})


class ArkEmbeddingProvider:
    provider = 'ark'

    def __init__(self, profile, *, client=None):
        self.dimensions = _dimensions(profile.get('dimensions', 1024))
        if self.dimensions not in {1024, 2048}:
            raise ValueError('Ark multimodal embeddings only support configured dimensions 1024 or 2048')
        self.key_env = profile.get('key_env', 'ARK_EMBEDDING_API_KEY')
        self.adapter = ManualEmbeddingClient(client, dimensions=self.dimensions,
            model=profile.get('model') or None, base_url=profile.get('base_url') or None,
            key_env=self.key_env, timeout_seconds=profile.get('timeout_seconds', 45))
        self.model = self.adapter.model

    def info(self):
        return {'provider': self.provider, 'execution_location': 'cloud', 'configured': bool(os.getenv(self.key_env)), 'model': self.model,
                'dimensions': self.dimensions, 'missing_fields': [] if os.getenv(self.key_env) else [self.key_env]}

    async def embed(self, text, *, query=False):
        if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 100000:
            raise ProviderError('invalid_request', 'Embedding text must be nonblank and at most 100000 UTF-8 bytes', provider=self.provider)
        key = os.getenv(self.key_env, '')
        if not key:
            raise ProviderError('not_configured', self.key_env + ' is not configured; no embedding request was made', provider=self.provider)
        try:
            result = await self.adapter.embed(text, query=query)
            if result.get('model') != self.model:
                raise ProviderError('invalid_response', 'Embedding response model differs from the configured index model', provider=self.provider)
            # Legacy Ark transport already normalizes; verify without changing its
            # existing normalization arithmetic or payload bytes.
            _normalize(result.get('vector'), self.dimensions, self.provider)
            return redact({**result, 'provider': self.provider}, secrets=(key,))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _failure(exc, self.provider, key) from None

    async def aclose(self):
        await self.adapter.aclose()


class OpenAICompatibleEmbeddingProvider:
    provider = 'openai_compatible'

    def __init__(self, profile, *, client=None):
        self.dimensions = _dimensions(profile.get('dimensions', 1024))
        self.model, self.base_url = profile.get('model') or '', profile.get('base_url') or ''
        if not isinstance(self.model, str) or not isinstance(self.base_url, str):
            raise ValueError('Embedding model/base_url must be strings')
        parsed = urlparse(self.base_url)
        if self.base_url and (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
                             or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Embedding base_url must be an HTTP(S) URL without credentials/query/fragment')
        self.model_env, self.base_url_env = profile.get('model_env'), profile.get('base_url_env')
        self.key_env = profile.get('key_env', '')
        self.require_api_key = profile.get('require_api_key', True)
        if type(self.require_api_key) is not bool:
            raise ValueError('Embedding require_api_key must be a boolean')
        self.timeout_seconds = profile.get('timeout_seconds', 45)
        if type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError('Embedding timeout must be positive and finite')
        self.client = client if client is not None else httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds, connect=10))
        self._owns_client, self._closed = client is None, False

    def info(self):
        missing = []
        if not self.base_url:
            missing.append(self.base_url_env or 'base_url')
        if not self.model:
            missing.append(self.model_env or 'model')
        if self.require_api_key and not os.getenv(self.key_env, ''):
            missing.append(self.key_env or 'key_env')
        return {'provider': self.provider, 'execution_location': 'cloud', 'configured': not missing, 'missing_fields': missing,
                'model': self.model, 'dimensions': self.dimensions}

    async def embed(self, text, *, query=False):
        if self._closed:
            raise ProviderError('unavailable', 'Embedding provider is closed', provider=self.provider)
        if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 100000:
            raise ProviderError('invalid_request', 'Embedding text must be nonblank and at most 100000 UTF-8 bytes', provider=self.provider)
        if not self.info()['configured']:
            raise ProviderError('not_configured', 'Embedding endpoint, model or required API key is missing', provider=self.provider,
                                details={'missing_fields': self.info()['missing_fields']})
        key = os.getenv(self.key_env, '')
        instruction = load_prompt('manual_embedding_query' if query else 'manual_embedding_document')
        body = {'model': self.model, 'dimensions': self.dimensions, 'encoding_format': 'float',
                'input': instruction + '\n\n' + text}
        started = time.perf_counter()
        try:
            async with self.client.stream('POST', self.base_url.rstrip('/') + '/embeddings', json=body,
                headers={'Authorization': 'Bearer ' + key} if key else {}, timeout=self.timeout_seconds) as response:
                response.raise_for_status()
                raw = bytearray()
                async for part in response.aiter_bytes():
                    raw.extend(part)
                    if len(raw) > 8 * 1024 * 1024:
                        raise ProviderError('invalid_response', 'Embedding response exceeds the size limit', provider=self.provider)
                data = json.loads(raw)
            if not isinstance(data, dict) or data.get('model') != self.model:
                raise ProviderError('invalid_response', 'Embedding response model differs from the configured index model', provider=self.provider)
            records = data.get('data')
            if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
                raise ProviderError('invalid_response', 'Embedding service must return exactly one vector', provider=self.provider)
            vector = _normalize(records[0].get('embedding'), self.dimensions, self.provider)
            return {'vector': vector, 'model': self.model, 'provider': self.provider,
                    'usage': redact(data.get('usage', {}), secrets=(key,)),
                    '_trace': redact({'request': body, 'response': data}, secrets=(key,)),
                    'elapsed_ms': round((time.perf_counter() - started) * 1000, 2)}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _failure(exc, self.provider, key) from None

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self.client.aclose()


def build_embedding(profile):
    if profile.get('provider', 'ark') == 'ark':
        return ArkEmbeddingProvider(profile)
    if profile.get('provider') == 'openai_compatible':
        return OpenAICompatibleEmbeddingProvider(profile)
    raise ValueError('Unsupported embedding provider: ' + str(profile.get('provider')))
