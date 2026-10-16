"""Read-only car-manual retrieval; model chooses when to use it."""
from backend.tool_text_config import tool_text
from backend.speech_config import feedback_text
import asyncio
from collections import OrderedDict
import json
import os
from pathlib import Path
import sqlite3
import shutil
import time
from uuid import uuid4

import numpy as np

from backend.models import utcnow
from backend.prompt_templates import load_prompt
from backend.settings import ROOT

DEFAULT_INDEX = ROOT / 'data/manuals/denza_d9.sqlite3'
PAGE_IMAGES_PENDING = feedback_text('manual.images_pending')
MANUAL_TOOLS = [{
    'type': 'function', 'function': {
        'name': 'search_manual',
        'description': tool_text('search_manual.description'),
        'parameters': {'type': 'object', 'properties': {
            'query': {'type': 'string', 'minLength': 1, 'maxLength': 800,
                      'description': tool_text('search_manual.parameters.query.description')},
            'max_results': {'type': 'integer', 'minimum': 1, 'maximum': 5}},
            'required': ['query'], 'additionalProperties': False}}}]
MANUAL_BY_NAME = {tool['function']['name']: tool for tool in MANUAL_TOOLS}


def load_index(path):
    if not path.is_file():
        raise RuntimeError('车书索引不存在，请先运行 python -m scripts.manual_index build。')
    try:
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            record = db.execute('SELECT value FROM metadata WHERE key=?', ('config',)).fetchone()
            meta = json.loads(record[0])
            rows = db.execute('SELECT id, page, section, text, vector FROM chunks ORDER BY id').fetchall()
        if not isinstance(meta, dict):
            raise ValueError('索引元信息无效')
        for key in ('source_pdf', 'source_sha256', 'embedding_model', 'notice',
                    'query_instruction', 'document_instruction'):
            if not isinstance(meta.get(key), str):
                raise ValueError('索引元信息缺失')
        if type(meta.get('pages')) is not int or meta['pages'] < 1:
            raise ValueError('索引页数无效')
        if len(meta['source_sha256']) != 64 or any(c not in '0123456789abcdef' for c in meta['source_sha256']):
            raise ValueError('索引来源标识无效')
        if not meta.get('ready') or not rows or any(row[4] is None for row in rows):
            raise ValueError('索引未完成')
        if any(type(row[1]) is not int or not 1 <= row[1] <= meta['pages']
               or not isinstance(row[2], str) or not isinstance(row[3], str) for row in rows):
            raise ValueError('索引片段无效')
        meta.setdefault('embedding_provider', 'ark')
        if not isinstance(meta['embedding_provider'], str) or not meta['embedding_provider']:
            raise ValueError('索引向量服务标识无效')
        dimensions = meta['dimensions']
        if type(dimensions) is not int or not 1 <= dimensions <= 65536:
            raise ValueError('维度无效')
        matrix = np.stack([np.frombuffer(row[4], dtype='<f4') for row in rows])
        if matrix.shape != (len(rows), dimensions) or not np.isfinite(matrix).all():
            raise ValueError('索引向量无效')
        norms = np.linalg.norm(matrix, axis=1)
        if not np.isfinite(norms).all() or np.any(norms == 0):
            raise ValueError('索引包含零向量')
        matrix = matrix / norms[:, None]
        passages = tuple({'chunk_id': row[0], 'page': row[1], 'section': row[2], 'text': row[3]}
                         for row in rows)
        return meta, passages, matrix
    except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError) as exc:
        raise RuntimeError('车书索引无效或未完成，请重新建库。') from exc


class ManualSearchClient:
    def __init__(self, path=None, embeddings=None):
        self.path = Path(path or os.getenv('MANUAL_INDEX_PATH', str(DEFAULT_INDEX)))
        if embeddings is None:
            from backend.services.factory import create_embedding_provider
            embeddings_client = create_embedding_provider()
        else:
            embeddings_client = embeddings
        self.embeddings = embeddings_client
        self.owns_embeddings = embeddings is None
        self._index = None
        self._load_lock = asyncio.Lock()
        self._cache = OrderedDict()
        self._status = {'ready': False, 'status': 'not_loaded'}
        self._image_slots = asyncio.Semaphore(2)
        self._page_locks = {}

    def info(self):
        return {'provider': feedback_text('manual.provider'), 'configured': self.embedding_info()['configured'],
                'model': self.embeddings.model, **self._status}

    def embedding_info(self):
        if hasattr(self.embeddings, 'info'):
            return self.embeddings.info()
        # Compatibility for existing injected test clients and legacy adapters.
        return {'configured': bool(os.getenv('ARK_EMBEDDING_API_KEY')), 'provider': 'ark',
                'missing_fields': [] if os.getenv('ARK_EMBEDDING_API_KEY') else ['ARK_EMBEDDING_API_KEY']}

    async def _load(self):
        async with self._load_lock:
            if self._index is None:
                try:
                    meta, passages, matrix = await asyncio.to_thread(load_index, self.path)
                    if (meta['embedding_provider'] != getattr(self.embeddings, 'provider', 'ark')
                            or meta['embedding_model'] != self.embeddings.model
                            or meta['dimensions'] != self.embeddings.dimensions
                            or meta['query_instruction'] != load_prompt('manual_embedding_query')
                            or meta['document_instruction'] != load_prompt('manual_embedding_document')):
                        raise RuntimeError('车书索引与向量模型或指令不一致，需要重建索引。')
                    self._index = (meta, passages, matrix)
                    self._status = {'ready': True, 'status': 'ready', 'pages': meta['pages'],
                                    'chunks': len(passages), 'title': Path(meta['source_pdf']).stem}
                except RuntimeError:
                    self._status = {'ready': False, 'status': 'unavailable'}
                    raise
        return self._index

    async def prepare(self):
        try:
            await self._load()
        except RuntimeError:
            pass
        return self.info()

    async def document(self):
        meta, _, _ = await self._load()
        path = Path(meta['source_pdf'])
        if path.suffix.lower() != '.pdf' or not await asyncio.to_thread(path.is_file):
            raise RuntimeError('原始车书 PDF 不可用。')
        return path

    async def page_image(self, manual_id, page):
        meta, _, _ = await self._load()
        if manual_id != meta['source_sha256'][:16] or type(page) is not int or not 1 <= page <= meta['pages']:
            raise ValueError('车书或页码不存在。')
        directory = ROOT / 'data/manuals/pages' / manual_id
        destination = directory / f'{page}.png'
        lock = self._page_locks.setdefault(page, asyncio.Lock())
        async with lock:
            if await asyncio.to_thread(destination.is_file):
                return destination
            executable = os.getenv('MANUAL_PDFTOPPM_BIN') or shutil.which('pdftoppm')
            if not executable:
                raise RuntimeError('PDF 页面预览不可用：未安装 pdftoppm。')
            source = await self.document()
            async with self._image_slots:
                await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
                prefix = directory / f'{page}-{uuid4().hex}'
                temporary = prefix.with_suffix('.png')
                process = None
                try:
                    process = await asyncio.create_subprocess_exec(
                        executable, '-f', str(page), '-l', str(page), '-singlefile',
                        '-scale-to', '1200', '-png', str(source), str(prefix),
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                    async with asyncio.timeout(5):
                        await process.communicate()
                    if process.returncode != 0 or not await asyncio.to_thread(temporary.is_file):
                        raise RuntimeError('PDF 页面预览渲染失败。')
                    await asyncio.to_thread(temporary.replace, destination)
                except (OSError, TimeoutError) as exc:
                    raise RuntimeError('PDF 页面预览暂不可用。') from exc
                finally:
                    if process is not None and process.returncode is None:
                        process.kill()
                        await process.wait()
                    await asyncio.to_thread(temporary.unlink, missing_ok=True)
        return destination

    async def search_manual(self, query, max_results=5):
        if not isinstance(query, str) or not query.strip() or len(query) > 800:
            raise ValueError('车书查询必须为 1 至 800 字的非空问题。')
        if type(max_results) is not int or not 1 <= max_results <= 5:
            raise ValueError('max_results 必须为 1 至 5。')
        # Cached evidence never hides a missing credential on subsequent calls.
        embedding_status = self.embedding_info()
        if not embedding_status['configured']:
            missing = '、'.join(embedding_status.get('missing_fields', []))
            raise RuntimeError((missing or 'Embedding 服务') + ' 未配置，无法检索车书。')
        meta, passages, matrix = await self._load()
        query = query.strip()
        cached = self._cache.get(query)
        embedding_trace = None
        if cached is None:
            embedded = await self.embeddings.embed(query, query=True)
            vector = np.asarray(embedded['vector'], dtype=np.float32)
            if vector.shape != (matrix.shape[1],) or not np.isfinite(vector).all() or not np.linalg.norm(vector):
                raise RuntimeError('问题向量无效，未执行检索。')
            vector = vector / np.linalg.norm(vector)
            embedding_ms = embedded['elapsed_ms']
            embedding_trace = embedded.get('_trace')
            # This cache is retrieval-service state, never actor/session state.
            self._cache[query] = vector
            while len(self._cache) > 32:
                self._cache.popitem(last=False)
        else:
            vector, embedding_ms = cached, 0
            self._cache.move_to_end(query)

        def rank():
            started = time.perf_counter()
            scores = matrix @ vector
            hits = [{**passages[int(index)], 'score': round(float(scores[index]), 5)}
                    for index in np.argsort(-scores)[:max_results]]
            return hits, round((time.perf_counter() - started) * 1000, 2)

        hits, local_ms = await asyncio.to_thread(rank)
        manual_id = meta['source_sha256'][:16]
        # Page images are for the result card only. Rendering them (pdftoppm,
        # up to seconds per cold page) must not delay the spoken answer, so the
        # caller attaches them afterwards with with_page_images().
        for hit in hits:
            hit['images'] = []
        return {'ok': True, 'kind': 'manual', 'provider': feedback_text('manual.provider'), 'query': query,
                'manual': {'title': Path(meta['source_pdf']).stem, 'pages': meta['pages'],
                           'id': manual_id, 'pdf_url': '/api/manual/pdf'},
                'hits': hits, 'evidence': 'manual_text', 'notice': meta['notice'],
                'queried_at': utcnow(), 'embedding_ms': embedding_ms,
                'local_search_ms': local_ms, 'cache_hit': cached is not None,
                'image_notice': PAGE_IMAGES_PENDING,
                '_embedding_trace': embedding_trace}

    async def with_page_images(self, result):
        """Copy of a search result whose hits list only pages that actually rendered."""
        manual_id = result['manual']['id']

        async def image_for_page(page):
            try:
                await self.page_image(manual_id, page)
            except (RuntimeError, ValueError):
                return []
            return [{'url': f'/api/manual/{manual_id}/pages/{page}.png', 'page': page,
                     'caption': feedback_text('manual.page_caption', page=page)}]

        pages = list(dict.fromkeys(hit['page'] for hit in result['hits']))
        images = dict(zip(pages, await asyncio.gather(*(image_for_page(page) for page in pages))))
        hits = [{**hit, 'images': images[hit['page']]} for hit in result['hits']]
        return {**result, 'hits': hits,
                'image_notice': '' if all(images.values()) else feedback_text('manual.images_unavailable')}

    async def aclose(self):
        if self.owns_embeddings:
            await self.embeddings.aclose()
