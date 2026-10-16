"""Guard real provider boundaries without contacting Ark in unit tests."""
import json
import math

import httpx
import pytest

from backend.manual_embeddings import ManualEmbeddingClient


async def test_text_only_query_and_document_use_separate_requests(monkeypatch):
    monkeypatch.setenv('ARK_EMBEDDING_API_KEY', 'test-embedding-secret')
    monkeypatch.setenv('ARK_API_KEY', 'different-talker-secret')
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'model': 'doubao-embedding-vision-251215',
            'data': {'embedding': [3, 4] + [0] * 1022},
            'usage': {'prompt_tokens_details': {'image_tokens': 0}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client = ManualEmbeddingClient(transport)
        passage = await client.embed('手册原文')
        await client.embed('用户问题', query=True)
    bodies = [json.loads(request.content) for request in requests]
    assert all(request.headers['Authorization'] == 'Bearer test-embedding-secret' for request in requests)
    assert [body['input'] for body in bodies] == [[{'type': 'text', 'text': '手册原文'}],
                                               [{'type': 'text', 'text': '用户问题'}]]
    assert bodies[0]['instructions'] != bodies[1]['instructions']
    assert math.isclose(sum(x * x for x in passage['vector']), 1)


async def test_missing_dedicated_credential_does_not_fall_back_to_talker(monkeypatch):
    monkeypatch.delenv('ARK_EMBEDDING_API_KEY', raising=False)
    monkeypatch.setenv('ARK_API_KEY', 'working-talker-credential')

    def unexpected(request):
        pytest.fail('Missing embedding key must not issue a request')

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as transport:
        with pytest.raises(RuntimeError) as error:
            await ManualEmbeddingClient(transport).embed('问题')
        assert 'ARK_EMBEDDING_API_KEY' in str(error.value)


@pytest.mark.parametrize('vector', [[1] * 512, [0] * 1024, [True] * 1024,
                                  [1e308] * 1024])
async def test_invalid_provider_vectors_are_rejected(monkeypatch, vector):
    monkeypatch.setenv('ARK_EMBEDDING_API_KEY', 'test-embedding-secret')

    def respond(request):
        return httpx.Response(200, json={'model': 'test', 'data': {'embedding': vector}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        with pytest.raises(RuntimeError) as error:
            await ManualEmbeddingClient(transport).embed('问题')
        assert '说明书向量化失败' in str(error.value)


async def test_transport_error_cannot_disclose_embedding_key(monkeypatch):
    key = 'embedding-secret-that-must-not-appear'
    monkeypatch.setenv('ARK_EMBEDDING_API_KEY', key)

    def fail(request):
        raise httpx.ConnectError('unexpected provider diagnostic: ' + key, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as transport:
        with pytest.raises(RuntimeError) as error:
            await ManualEmbeddingClient(transport).embed('问题')
    assert key not in str(error.value)
    assert '[redacted]' in str(error.value)
