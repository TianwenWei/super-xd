import asyncio
import copy
import json
import sqlite3

import numpy as np
import pytest

from super_xd.demo.tasks.database import Database
from super_xd.demo.resources.manual_search import ManualSearchClient
from super_xd.nlp.prompts.templates import load_prompt
from super_xd.demo.session.actor import SessionActor
from tests.support.session import FakeArk, call, wait_for


class Embeddings:
    model = 'doubao-embedding-vision-251215'
    dimensions = 1024

    def __init__(self):
        self.calls = 0

    async def embed(self, text, *, query=False):
        self.calls += 1
        return {'vector': [1.] + [0.] * 1023, 'model': self.model, 'elapsed_ms': 1,
                '_trace': {'request': {'input': [{'type': 'text', 'text': text}]}, 'response': {'data': 'test-vector'}}}


@pytest.fixture
def index_file(tmp_path, monkeypatch):
    monkeypatch.setenv('ARK_EMBEDDING_API_KEY', 'manual-test-secret')
    monkeypatch.setattr('super_xd.demo.resources.manual_search.shutil.which', lambda _: None)
    path = tmp_path / 'manual.sqlite3'
    meta = {'ready': True, 'dimensions': 1024, 'pages': 3, 'source_pdf': str(tmp_path / 'manual.pdf'),
            'source_sha256': 'a' * 64, 'embedding_model': Embeddings.model, 'notice': '测试手册',
            'query_instruction': load_prompt('manual_embedding_query'),
            'document_instruction': load_prompt('manual_embedding_document')}
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)')
        db.execute('INSERT INTO metadata VALUES (?,?)', ('config', json.dumps(meta)))
        db.execute('CREATE TABLE chunks(id INTEGER PRIMARY KEY,page INTEGER,section TEXT,text TEXT,vector BLOB)')
        for identity, page, text, vector in [(1, 2, '相关原文', [1., 0.]), (2, 3, '其他原文', [0., 1.])]:
            db.execute('INSERT INTO chunks VALUES (?,?,?,?,?)',
                       (identity, page, '测试章节', text, np.asarray(vector + [0.] * 1022, dtype='<f4').tobytes()))
    return path, meta


async def test_retrieval_keeps_page_evidence_and_reuses_embedding(index_file):
    path, _ = index_file
    embedding = Embeddings()
    client = ManualSearchClient(path, embedding)
    first = await client.search_manual('查询', 1)
    second = await client.search_manual('查询', 2)
    assert first['hits'][0]['page'] == 2 and first['hits'][0]['text'] == '相关原文'
    assert first['hits'][0]['images'] == [] and first['image_notice']
    assert first['evidence'] == 'manual_text' and first['_embedding_trace']
    assert second['cache_hit'] and second['_embedding_trace'] is None and embedding.calls == 1
    assert len(second['hits']) == 2 and client.info()['ready']


@pytest.mark.parametrize('fault', ['missing_vector', 'not_ready', 'wrong_model', 'nonfinite_vector', 'null_metadata', 'missing_metadata'])
async def test_invalid_index_never_calls_embedding(index_file, fault):
    path, meta = index_file
    with sqlite3.connect(path) as db:
        if fault == 'missing_vector':
            db.execute('UPDATE chunks SET vector=NULL WHERE id=1')
        elif fault == 'nonfinite_vector':
            db.execute('UPDATE chunks SET vector=? WHERE id=1', (np.full(1024, np.nan, dtype='<f4').tobytes(),))
        elif fault in {'null_metadata', 'missing_metadata'}:
            if fault == 'missing_metadata':
                meta.pop('query_instruction')
            db.execute('UPDATE metadata SET value=?', (json.dumps(None if fault == 'null_metadata' else meta),))
        else:
            meta['ready' if fault == 'not_ready' else 'embedding_model'] = False if fault == 'not_ready' else 'other-model'
            db.execute('UPDATE metadata SET value=?', (json.dumps(meta),))
    embedding = Embeddings()
    with pytest.raises(RuntimeError):
        await ManualSearchClient(path, embedding).search_manual('查询')
    assert embedding.calls == 0
    assert (await ManualSearchClient(path, embedding).prepare())['ready'] is False


async def test_cached_query_does_not_hide_missing_key(index_file, monkeypatch):
    path, _ = index_file
    client = ManualSearchClient(path, Embeddings())
    await client.search_manual('查询')
    monkeypatch.delenv('ARK_EMBEDDING_API_KEY')
    with pytest.raises(RuntimeError) as error:
        await client.search_manual('查询')
    assert 'ARK_EMBEDDING_API_KEY' in str(error.value)


@pytest.mark.parametrize('identity,page', [('other', 2), ('a' * 16, 0), ('a' * 16, 4)])
async def test_image_cannot_serve_other_documents_or_invalid_pages(index_file, identity, page):
    with pytest.raises(ValueError):
        await ManualSearchClient(index_file[0], Embeddings()).page_image(identity, page)


class FakeManual:
    def __init__(self, *, late=False, error=None):
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.late, self.error, self.requests = late, error, []

    async def search_manual(self, **arguments):
        self.requests.append(arguments)
        self.started.set()
        if self.late:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                pass
        if self.error:
            raise self.error
        return {'ok': True, 'kind': 'manual', 'evidence': 'manual_text', 'provider': '车书',
                'manual': {'title': '测试手册', 'pages': 10},
                'hits': [{'chunk_id': 1, 'page': 8, 'text': '真实工具原文', 'images': []}],
                '_embedding_trace': {'request': {'input': arguments['query']}, 'response': {'model': 'test'}}}


@pytest.fixture
async def manual_actor(tmp_path):
    actors = []
    def make(responses, manual=None):
        ark, manual = FakeArk(responses), manual or FakeManual()
        actor = SessionActor(str(len(actors)), Database(tmp_path / f'{len(actors)}.sqlite'), ark, manual=manual)
        actors.append(actor)
        return actor, ark, manual
    yield make
    await asyncio.gather(*(actor.close() for actor in actors))


async def test_manual_result_reaches_same_talker_with_sources_and_no_side_effects(manual_actor):
    actor, ark, manual = manual_actor([
        {'content': '我去翻一下车书。', 'tool_calls': [call('search_manual', {'query': '功能使用方法'})]},
        {'content': '根据车书原文，可以这样使用。'}])
    queue = await actor.attach()
    await queue.get()
    before = copy.deepcopy(actor.vehicle)
    await actor.post({'type': 'input', 'text': '看看车书怎么说'})
    _, events = await wait_for(queue, 'stream.done')
    assert len(ark.requests) == 2 and manual.requests == [{'query': '功能使用方法'}]
    assert actor.vehicle == before and actor.db.list_tasks(actor.id) == []
    assert not any(event['type'] == 'vehicle.command' for event in events)
    result = next(event['result'] for event in events if event['type'] == 'lookup.result')
    assert result['hits'][0]['page'] == 8 and '_embedding_trace' not in result
    tool_message = next(m for m in reversed(ark.requests[1]['messages']) if m['role'] == 'tool')
    assert json.loads(tool_message['content'])['hits'][0]['text'] == '真实工具原文'
    await actor.trace_queue.join()
    traces = actor.db.traces_after(actor.id, 0, limit=200)
    assert any(event['kind'] == 'embedding.completed' for event in traces)


async def test_interrupted_manual_result_and_embedding_are_trace_only(manual_actor):
    actor, _, manual = manual_actor([
        {'tool_calls': [call('search_manual', {'query': '旧问题'})]}, {'content': '你好。'}], FakeManual(late=True))
    queue = await actor.attach()
    await queue.get()
    await actor.post({'type': 'input', 'text': '查车书'})
    await manual.started.wait()
    old = actor.current
    await actor.post({'type': 'input', 'text': '不用了，聊别的'})
    _, events = await wait_for(queue, 'stream.done')
    await actor.queue.join()
    await actor.trace_queue.join()
    assert old.lookup_results == [] and not any(event['type'] == 'lookup.result' for event in events)
    assert any(event['kind'] == 'embedding.completed' and event['status'] == 'stale'
               for event in actor.db.traces_after(actor.id, 0, limit=200))


async def test_manual_provider_error_is_explicit_and_redacted(manual_actor, monkeypatch):
    monkeypatch.setenv('ARK_EMBEDDING_API_KEY', 'private-manual-key')
    actor, _, _ = manual_actor([
        {'tool_calls': [call('search_manual', {'query': '问题'})]}, {'content': '这次查不到。'}],
        FakeManual(error=RuntimeError('private-manual-key unavailable')))
    queue = await actor.attach()
    await queue.get()
    await actor.post({'type': 'input', 'text': '查车书'})
    _, events = await wait_for(queue, 'stream.done')
    result = next(event['result'] for event in events if event['type'] == 'lookup.result')
    assert result['ok'] is False and 'private-manual-key' not in result['reason']


@pytest.mark.parametrize('arguments', [{'query': ''}, {'query': 'q', 'max_results': 6}, {'query': 'q', 'extra': True}])
async def test_invalid_manual_arguments_never_execute(manual_actor, arguments):
    actor, _, manual = manual_actor([{'tool_calls': [call('search_manual', arguments)]}, {'content': '请补充问题。'}])
    queue = await actor.attach()
    await queue.get()
    await actor.post({'type': 'input', 'text': '查车书'})
    await wait_for(queue, 'stream.done')
    assert not manual.requests


async def test_search_returns_before_page_images_and_lists_only_rendered_pages(index_file, monkeypatch):
    path, _ = index_file
    client = ManualSearchClient(path, Embeddings())
    rendered = []

    async def page_image(manual_id, page):
        rendered.append(page)
        if page == 3:
            raise RuntimeError('render failed')
        return path

    monkeypatch.setattr(client, 'page_image', page_image)
    result = await client.search_manual('查询', 2)
    assert not rendered and all(hit['images'] == [] for hit in result['hits']) and result['image_notice']
    updated = await client.with_page_images(result)
    images = {hit['page']: hit['images'] for hit in updated['hits']}
    assert images[2][0]['url'].endswith('/pages/2.png') and images[3] == []
    assert updated['image_notice'] and result['hits'][0]['images'] == []  # original untouched


class ImagingManual(FakeManual):
    def __init__(self):
        super().__init__()
        self.release_images = asyncio.Event()

    async def with_page_images(self, result):
        await self.release_images.wait()
        return {**result, 'hits': [{**hit, 'images': [{'url': '/api/manual/x/pages/8.png', 'page': 8}]}
                                   for hit in result['hits']], 'image_notice': ''}


async def test_answer_is_not_held_for_page_images_and_card_is_updated_later(manual_actor):
    manual = ImagingManual()
    actor, ark, _ = manual_actor([
        {'tool_calls': [call('search_manual', {'query': '功能使用方法'})]},
        {'content': '根据车书原文，可以这样使用。'}], manual)
    queue = await actor.attach()
    await queue.get()
    await actor.post({'type': 'input', 'text': '看看车书怎么说'})
    done, events = await wait_for(queue, 'stream.done')  # Images still blocked here.
    first = [e for e in events if e['type'] == 'lookup.result']
    assert done['text'] == '根据车书原文，可以这样使用。' and first[0]['result']['hits'][0]['images'] == []
    manual.release_images.set()
    update, _ = await wait_for(queue, 'lookup.result')
    assert update['call_id'] == first[0]['call_id'] and update['result']['hits'][0]['images'][0]['page'] == 8
    assert all(m.get('role') != 'tool' or '/pages/8.png' not in m['content'] for m in ark.requests[-1]['messages'])
