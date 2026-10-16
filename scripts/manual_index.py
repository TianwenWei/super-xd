"""Standalone single-manual SQLite vector index; no changes to actor routing."""
import argparse
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time

import numpy as np
from pypdf import PdfReader

from backend.services.factory import create_embedding_provider
from backend.prompt_templates import load_prompt
from backend.manual_search import load_index
from backend.settings import ROOT

DEFAULT_PDF = ROOT / 'output/pdf/腾势D9用户手册-中文右舵版.pdf'
DEFAULT_DB = ROOT / 'data/manuals/denza_d9.sqlite3'


def split_page(text, size=600, overlap=100):
    text = '\n'.join(' '.join(line.split()) for line in text.splitlines() if line.strip())
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = max(text.rfind('。', start + size // 2, end),
                           text.rfind('\n', start + size // 2, end))
            if boundary >= 0:
                end = boundary + 1
        yield text[start:end]
        if end == len(text):
            break
        start = end - overlap


def extract(pdf):
    reader = PdfReader(pdf)
    bookmarks = []

    def walk(items, parents=()):
        previous = parents
        for item in items:
            if isinstance(item, list):
                walk(item, previous)
            else:
                previous = parents + (item.title,)
                page = reader.get_destination_page_number(item)
                if page is not None:
                    bookmarks.append((page + 1, ' / '.join(previous)))

    walk(reader.outline)
    bookmarks.sort(key=lambda item: item[0])
    rows = []
    for number, page in enumerate(reader.pages, 1):
        section = next((title for n, title in reversed(bookmarks) if n <= number), '')
        for chunk in split_page(page.extract_text() or ''):
            rows.append((number, section, chunk))
    if not rows:
        raise RuntimeError('PDF 未提取出文字；本脚本不使用 OCR 或假内容。')
    return rows, len(reader.pages)


def read_index(path):
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        meta = json.loads(db.execute('SELECT value FROM metadata WHERE key=?', ('config',)).fetchone()[0])
        rows = db.execute('SELECT id, page, section, text, vector FROM chunks ORDER BY id').fetchall()
    return meta, rows


def initialize(pdf, path, client):
    meta = {'source_pdf': str(pdf.resolve()), 'source_sha256': sha256(pdf.read_bytes()).hexdigest(),
            'embedding_model': client.model, 'embedding_provider': getattr(client, 'provider', 'ark'), 'dimensions': client.dimensions,
            'query_instruction': load_prompt('manual_embedding_query'),
            'document_instruction': load_prompt('manual_embedding_document'),
            'chunk_size': 600, 'overlap': 100,
            'notice': '来源为腾势 D9 中文右舵版手册；配置可能与模拟车辆及大陆车型不同。仅提取文字，未识别插图。'}
    if path.exists():
        old, rows = read_index(path)
        old.setdefault('embedding_provider', 'ark')
        if any(old.get(key) != value for key, value in meta.items()):
            raise RuntimeError('已有索引的来源、模型或切片配置不同，请指定新的 --db 文件。')
        return old, [row for row in rows if row[4] is None]
    chunks, pages = extract(pdf)
    meta.update(pages=pages, chunks=len(chunks), ready=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute('CREATE TABLE chunks (id INTEGER PRIMARY KEY, page INTEGER, section TEXT, text TEXT, vector BLOB)')
        db.executemany('INSERT INTO chunks(page, section, text) VALUES(?,?,?)', chunks)
        db.execute('INSERT INTO metadata VALUES(?,?)', ('config', json.dumps(meta, ensure_ascii=False)))
    return meta, read_index(path)[1]


def write_vectors(path, records):
    with sqlite3.connect(path) as db:
        db.executemany('UPDATE chunks SET vector=? WHERE id=?', records)


async def build(args):
    client = create_embedding_provider()
    try:
        meta, pending = await asyncio.to_thread(initialize, args.pdf, args.db, client)
        print(json.dumps({'pages': meta['pages'], 'chunks': meta['chunks'], 'pending': len(pending)}, ensure_ascii=False), flush=True)
        started = time.perf_counter()
        tokens = 0
        # Commit small batches so an interrupted build can resume without
        # repeating successful paid embedding requests. Partial indices refuse queries.
        for start in range(0, len(pending), 4):
            batch = pending[start:start + 4]
            results = await asyncio.gather(*[client.embed(row[2] + '\n' + row[3]) for row in batch])
            records = [(np.asarray(result['vector'], dtype='<f4').tobytes(), row[0])
                       for row, result in zip(batch, results)]
            tokens += sum(result['usage'].get('total_tokens', 0) for result in results)
            await asyncio.to_thread(write_vectors, args.db, records)
            if start % 40 == 0 or start + 4 >= len(pending):
                print(json.dumps({'embedded': min(start + 4, len(pending)), 'pending_at_start': len(pending),
                                  'tokens_this_run': tokens}), flush=True)
        meta['ready'] = True

        def finish():
            with sqlite3.connect(args.db) as db:
                db.execute('UPDATE metadata SET value=? WHERE key=?', (json.dumps(meta, ensure_ascii=False), 'config'))

        await asyncio.to_thread(finish)
        print(json.dumps({'ready': True, 'db': str(args.db.resolve()), 'bytes': args.db.stat().st_size,
                          'elapsed_seconds': round(time.perf_counter() - started, 2), 'tokens_this_run': tokens}), flush=True)
    finally:
        await client.aclose()


async def search(args):
    checked_meta, _, checked_matrix = await asyncio.to_thread(load_index, args.db)
    meta, rows = await asyncio.to_thread(read_index, args.db)
    if checked_meta != {**meta, 'embedding_provider': meta.get('embedding_provider', 'ark')}:
        raise RuntimeError('索引在查询准备期间发生变化，请重试。')
    if not meta.get('ready') or any(row[4] is None for row in rows):
        raise RuntimeError('索引尚未建完，请先完成 build。')
    client = create_embedding_provider()
    try:
        if (getattr(client, 'provider', 'ark') != meta.get('embedding_provider', 'ark')
                or client.dimensions != meta['dimensions']
                or client.model != meta['embedding_model']
                or load_prompt('manual_embedding_query') != meta['query_instruction']
                or load_prompt('manual_embedding_document') != meta['document_instruction']):
            raise RuntimeError('查询模型或指令与建库时不一致，需要重建索引。')
        result = await client.embed(args.question, query=True)
    finally:
        await client.aclose()

    def rank():
        started = time.perf_counter()
        matrix = checked_matrix
        scores = matrix @ np.asarray(result['vector'], dtype=np.float32)
        hits = []
        for index in np.argsort(-scores)[:args.top_k]:
            row = rows[int(index)]
            hits.append({'chunk_id': row[0], 'page': row[1], 'section': row[2],
                         'text': row[3], 'score': round(float(scores[index]), 5)})
        return hits, round((time.perf_counter() - started) * 1000, 2)

    hits, local_ms = await asyncio.to_thread(rank)
    report = {'query': args.question, 'embedding_model': result['model'], 'evidence': 'manual_text',
              'source_pdf': meta['source_pdf'], 'notice': meta['notice'],
              'embedding_ms': result['elapsed_ms'], 'local_search_ms': local_ms, 'hits': hits}
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    build_parser = sub.add_parser('build')
    build_parser.add_argument('--pdf', type=Path, default=DEFAULT_PDF)
    build_parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    query_parser = sub.add_parser('query')
    query_parser.add_argument('question')
    query_parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    query_parser.add_argument('--top-k', type=int, choices=range(1, 11), default=5)
    args = parser.parse_args()
    asyncio.run(build(args) if args.command == 'build' else search(args))


if __name__ == '__main__':
    main()
