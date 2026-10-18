"""Explicit HTTP fixtures validate embeddings, never substitute a live provider."""
import json
import math
import sqlite3

import httpx
import numpy as np
import pytest

from super_xd.demo.resources.manual_search import ManualSearchClient, load_index
from super_xd.nlp.prompts.templates import load_prompt
from super_xd.nlp.contracts.providers import ProviderError
from super_xd.demo.resources.embedding import ArkEmbeddingProvider, OpenAICompatibleEmbeddingProvider


def profile(**values):
    return {'provider': 'openai_compatible', 'base_url': 'http://127.0.0.1:9000/v1',
            'model': 'local-embedding', 'dimensions': 3, 'key_env': 'TEST_EMBEDDING_KEY',
            'require_api_key': True, **values}


async def test_compatible_sends_real_standard_fields_and_distinct_instruction_inputs(monkeypatch):
    monkeypatch.setenv('TEST_EMBEDDING_KEY', 'dedicated-secret')
    requests = []
    def handler(request):
        requests.append(request)
        assert str(request.url) == 'http://127.0.0.1:9000/v1/embeddings'
        assert request.headers['authorization'] == 'Bearer dedicated-secret'
        payload = json.loads(request.content)
        assert payload['model'] == 'local-embedding' and payload['dimensions'] == 3 and payload['encoding_format'] == 'float'
        return httpx.Response(200, json={'model': 'local-embedding', 'data': [{'index': 0, 'embedding': [3, 4, 0]}],
                                       'usage': {'total_tokens': 4}, 'api_key': 'dedicated-secret'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = OpenAICompatibleEmbeddingProvider(profile(), client=http)
        document = await provider.embed('原文')
        query = await provider.embed('问题', query=True)
        assert document['vector'] == [.6, .8, 0] and query['vector'] == [.6, .8, 0]
        assert document['provider'] == 'openai_compatible' and document['model'] == provider.model
        assert document['usage']['total_tokens'] == 4
        assert document['_trace']['request'] == json.loads(requests[0].content)
        assert 'dedicated-secret' not in json.dumps(document)
        assert json.loads(requests[0].content)['input'] == load_prompt('manual_embedding_document') + '\n\n原文'
        assert json.loads(requests[1].content)['input'] == load_prompt('manual_embedding_query') + '\n\n问题'
        await provider.aclose()
        assert not http.is_closed


async def test_embedding_missing_credentials_does_not_borrow_talker_key(monkeypatch):
    monkeypatch.delenv('TEST_EMBEDDING_KEY', raising=False)
    monkeypatch.setenv('ARK_API_KEY', 'talker-only')
    def forbidden(request):
        raise AssertionError('Must not query without required embedding credentials')
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        provider = OpenAICompatibleEmbeddingProvider(profile(), client=http)
        assert provider.info()['configured'] is False
        with pytest.raises(ProviderError) as caught:
            await provider.embed('问题')
        assert caught.value.code == 'not_configured' and 'talker-only' not in str(caught.value)


@pytest.mark.parametrize('vector', [[1, 0], [0, 0, 0], [True, 0, 0], [math.inf, 0, 0], [math.nan, 0, 0]])
async def test_invalid_vectors_fail_with_no_searchable_fake_embedding(monkeypatch, vector):
    monkeypatch.setenv('TEST_EMBEDDING_KEY', 'dedicated')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            text=json.dumps({'model': 'local-embedding', 'data': [{'embedding': vector}]})))) as http:
        provider = OpenAICompatibleEmbeddingProvider(profile(), client=http)
        with pytest.raises(ProviderError) as caught:
            await provider.embed('问题')
        assert caught.value.code == 'invalid_response'


async def test_provider_model_response_mismatch_is_explicit(monkeypatch):
    monkeypatch.setenv('TEST_EMBEDDING_KEY', 'dedicated')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            json={'model': 'other', 'data': [{'embedding': [1, 0, 0]}]}))) as http:
        with pytest.raises(ProviderError) as caught:
            await OpenAICompatibleEmbeddingProvider(profile(), client=http).embed('问题')
        assert caught.value.code == 'invalid_response'


def index(path, provider='openai_compatible', dimensions=3):
    meta = {'ready': True, 'dimensions': dimensions, 'pages': 1, 'source_pdf': '/manual.pdf',
            'source_sha256': 'a' * 64, 'embedding_model': 'local-embedding', 'notice': '显式测试资料',
            'query_instruction': load_prompt('manual_embedding_query'),
            'document_instruction': load_prompt('manual_embedding_document')}
    if provider is not None:
        meta['embedding_provider'] = provider
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)')
        db.execute('INSERT INTO metadata VALUES (?,?)', ('config', json.dumps(meta)))
        db.execute('CREATE TABLE chunks(id INTEGER PRIMARY KEY,page INTEGER,section TEXT,text TEXT,vector BLOB)')
        db.execute('INSERT INTO chunks VALUES (1,1,?,?,?)', ('章节', '原文', np.asarray([1] + [0] * (dimensions - 1), dtype='<f4').tobytes()))
    return meta


async def test_index_provider_mismatch_fails_before_query(tmp_path):
    path = tmp_path / 'manual.sqlite3'
    index(path, provider='ark')
    class Provider:
        model, dimensions, provider = 'local-embedding', 3, 'openai_compatible'
        def info(self):
            return {'configured': True}
        async def embed(self, *args, **kwargs):
            raise AssertionError('Mismatched index cannot query embeddings')
    with pytest.raises(RuntimeError):
        await ManualSearchClient(path, Provider()).search_manual('查询')


def test_old_index_defaults_only_to_ark_and_allows_declared_compatible_dimensions(tmp_path):
    old = tmp_path / 'old.sqlite3'; index(old, provider=None)
    assert load_index(old)[0]['embedding_provider'] == 'ark'
    current = tmp_path / 'current.sqlite3'; index(current)
    assert load_index(current)[2].shape == (1, 3)


def test_ark_does_not_claim_unsupported_dimensions():
    with pytest.raises(ValueError):
        ArkEmbeddingProvider({'provider': 'ark', 'dimensions': 3})


async def test_ark_wrapper_preserves_original_request_and_normalization(monkeypatch):
    from super_xd.demo.resources.manual_embeddings import ManualEmbeddingClient
    monkeypatch.setenv('ARK_EMBEDDING_API_KEY', 'dedicated-ark-key')
    requests = []
    model = 'doubao-embedding-vision-251215'
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'model': model, 'data': {'embedding': [3, 4] + [0] * 1022}, 'usage': {'total_tokens': 2}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        old, wrapper = ManualEmbeddingClient(http), ArkEmbeddingProvider({'provider': 'ark'}, client=http)
        for query in (False, True):
            left = await old.embed('实际输入', query=query)
            right = await wrapper.embed('实际输入', query=query)
            assert requests[-2].content == requests[-1].content
            assert requests[-2].headers['authorization'] == requests[-1].headers['authorization']
            assert left['vector'] == right['vector'] and left['_trace'] == right['_trace']
        await wrapper.aclose()
        assert not http.is_closed


async def test_custom_ark_secret_is_removed_before_diagnostic_truncation(monkeypatch):
    key = 'long-dedicated-credential-' + 'x' * 400
    monkeypatch.setenv('CUSTOM_EMBEDDING_KEY', key)
    def fail(request):
        raise httpx.ConnectError('connection diagnostic: ' + key, request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
        provider = ArkEmbeddingProvider({'provider': 'ark', 'key_env': 'CUSTOM_EMBEDDING_KEY'}, client=http)
        with pytest.raises(ProviderError) as caught:
            await provider.embed('问题')
        assert caught.value.code == 'unavailable'
        assert 'long-dedicated-credential' not in str(caught.value)
        assert '[redacted]' in str(caught.value)
