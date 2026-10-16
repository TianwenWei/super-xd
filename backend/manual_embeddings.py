"""Text-only manual embeddings with credentials separate from Talker."""
import math
import os
import time

import httpx

from backend.models import sanitized_error
from backend.prompt_templates import load_prompt


class ManualEmbeddingClient:
    def __init__(self, client=None, *, dimensions=1024):
        if type(dimensions) is not int or dimensions not in {1024, 2048}:
            raise ValueError('dimensions 必须为 1024 或 2048。')
        self.dimensions = dimensions
        self.model = os.getenv('ARK_EMBEDDING_MODEL', 'doubao-embedding-vision-251215')
        self.base_url = os.getenv('ARK_EMBEDDING_BASE_URL',
                                  'https://ark.cn-beijing.volces.com/api/v3').rstrip('/')
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10))

    async def embed(self, text, *, query=False):
        key = os.getenv('ARK_EMBEDDING_API_KEY')
        if not key:
            raise RuntimeError('ARK_EMBEDDING_API_KEY 未配置，未生成向量。')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('需要非空文字。')
        if len(text.encode('utf-8')) > 100000:
            raise ValueError('单段文字超过 100000 字节，请先切片。')
        started = time.perf_counter()
        # A multimodal request produces ONE vector for its combined input.
        # Separate passages therefore need separate requests, even for text.
        body = {'model': self.model, 'dimensions': self.dimensions,
                'encoding_format': 'float', 'input': [{'type': 'text', 'text': text}],
                'instructions': load_prompt('manual_embedding_query' if query else 'manual_embedding_document')}
        try:
            response = await self.client.post(self.base_url + '/embeddings/multimodal',
                headers={'Authorization': 'Bearer ' + key}, json=body)
            response.raise_for_status()
            data = response.json()
            vector = data['data']['embedding']
            if (not isinstance(vector, list) or len(vector) != self.dimensions
                    or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector)):
                raise ValueError('向量格式、维度或数值无效')
            norm = math.sqrt(sum(x * x for x in vector))
            if not math.isfinite(norm) or norm == 0:
                raise ValueError('向量不能归一化')
            return {'vector': [x / norm for x in vector], 'model': data['model'],
                    'usage': data.get('usage', {}),
                    '_trace': {'request': body, 'response': data},
                    'elapsed_ms': round((time.perf_counter() - started) * 1000, 2)}
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            message = sanitized_error(exc).replace(key, '[redacted]')
            raise RuntimeError('说明书向量化失败：' + message) from None

    async def aclose(self):
        await self.client.aclose()
