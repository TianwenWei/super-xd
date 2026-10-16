"""Manual CP fixtures validate real source metadata; no local page is invented."""
from copy import deepcopy
import json

import httpx
import pytest

from backend.services.contracts import ProviderError
from backend.services.manual import RemoteManualProvider, ManualProvider


def profile(**values):
    return {'provider': 'http_gateway', 'base_url': 'https://manual-cp.example',
            'key_env': 'MANUAL_TEST_KEY', **values}


def response():
    return {'ok': True, 'manual': {'id': 'manual-v1', 'title': 'CP实际手册', 'pages': 120,
                                  'pdf_url': 'https://manual-cp.example/documents/manual-v1.pdf'},
            'hits': [{'chunk_id': 'section-1', 'section': '车窗使用', 'page': 12, 'text': 'CP返回的实际车书片段。'}],
            'notice': '当前车型资料', 'queried_at': '2026-10-16T12:00:00+00:00'}


async def test_remote_manual_keeps_actual_source_and_never_creates_local_images(monkeypatch):
    monkeypatch.setenv('MANUAL_TEST_KEY', 'manual-only-secret')
    requests = []
    actual = response()
    def handler(request):
        requests.append(request)
        assert str(request.url) == 'https://manual-cp.example/v1/manual/search'
        assert request.headers['authorization'] == 'Bearer manual-only-secret'
        assert json.loads(request.content) == {'query': '怎么使用车窗', 'max_results': 5}
        return httpx.Response(200, json=actual)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = RemoteManualProvider(profile(), client=http)
        assert isinstance(provider, ManualProvider)
        status = await provider.prepare()
        assert not requests and status['availability'] == 'not_verified'
        result = await provider.search_manual('怎么使用车窗')
        assert result['kind'] == 'manual' and result['hits'][0]['text'] == actual['hits'][0]['text']
        assert result['manual']['pdf_url'] == actual['manual']['pdf_url']
        assert result['hits'][0]['source_url'] == actual['manual']['pdf_url']
        assert result['hits'][0]['images'] == []
        assert result['_provider_trace']['response'] == actual
        assert not any('/api/manual' in str(value) for value in result.values())
        assert await provider.with_page_images(result) == result and len(requests) == 1
        with pytest.raises(ProviderError) as pdf:
            await provider.document()
        with pytest.raises(ProviderError) as image:
            await provider.page_image('manual-v1', 12)
        assert pdf.value.code == image.value.code == 'unsupported_capability'
        await provider.aclose()
        assert not http.is_closed


async def test_remote_manual_missing_key_is_explicit_and_never_uses_embedding_key(monkeypatch):
    monkeypatch.delenv('MANUAL_TEST_KEY', raising=False)
    monkeypatch.setenv('ARK_EMBEDDING_API_KEY', 'embedding-only')
    def forbidden(request):
        raise AssertionError('Missing independent manual key must not contact CP')
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        provider = RemoteManualProvider(profile(), client=http)
        assert not (await provider.prepare())['configured']
        with pytest.raises(ProviderError) as caught:
            await provider.search_manual('问题')
        assert caught.value.code == 'not_configured'


@pytest.mark.parametrize('fault', ['missing_source', 'wrong_page', 'empty_section', 'empty_text', 'unsafe_pdf', 'unsafe_image', 'wrong_manual', 'unknown_source'])
async def test_remote_manual_rejects_untraceable_or_invalid_excerpts(monkeypatch, fault):
    monkeypatch.setenv('MANUAL_TEST_KEY', 'key')
    actual = response()
    if fault == 'missing_source': actual['manual'].pop('pdf_url')
    if fault == 'wrong_page': actual['hits'][0]['page'] = 121
    if fault == 'empty_section': actual['hits'][0]['section'] = ''
    if fault == 'empty_text': actual['hits'][0]['text'] = ''
    if fault == 'unsafe_pdf': actual['manual']['pdf_url'] = 'file:///private/not-a-source.pdf'
    if fault == 'unsafe_image': actual['hits'][0]['images'] = [{'url': '/api/manual/fake/pages/12.png', 'page': 12}]
    if fault == 'wrong_manual': actual['hits'][0]['manual_id'] = 'another-manual'
    if fault == 'unknown_source': actual['hits'][0]['source_id'] = 'unknown'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=actual))) as http:
        with pytest.raises(ProviderError) as caught:
            await RemoteManualProvider(profile(), client=http).search_manual('问题')
        assert caught.value.code == 'invalid_response'


async def test_remote_manual_source_ids_and_provider_supplied_images_remain_exact(monkeypatch):
    monkeypatch.setenv('MANUAL_TEST_KEY', 'key')
    actual = response()
    actual['manual'].pop('pdf_url')
    actual['sources'] = [{'id': 'source1', 'title': '真实来源', 'url': 'https://manual-cp.example/original'}]
    actual['hits'][0]['source_id'] = 'source1'
    actual['hits'][0]['images'] = [{'url': 'https://manual-cp.example/rendered/page12.png', 'page': 12, 'caption': 'CP真实页图'}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=actual))) as http:
        result = await RemoteManualProvider(profile(), client=http).search_manual('问题')
        assert result['hits'][0]['source_url'] == actual['sources'][0]['url']
        assert result['hits'][0]['images'] == actual['hits'][0]['images']
        assert 'pdf_url' not in result['manual']


async def test_empty_remote_results_do_not_invent_a_document(monkeypatch):
    monkeypatch.setenv('MANUAL_TEST_KEY', 'key')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'ok': True, 'hits': []}))) as http:
        result = await RemoteManualProvider(profile(), client=http).search_manual('问题')
        assert result['hits'] == [] and 'manual' not in result
        assert result['evidence'] == 'no_manual_matches'


async def test_remote_manual_transport_error_and_raw_diagnostics_are_redacted(monkeypatch):
    monkeypatch.setenv('MANUAL_TEST_KEY', 'secret-value')
    def handler(request):
        raise httpx.ConnectError('private secret-value', request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ProviderError) as caught:
            await RemoteManualProvider(profile(), client=http).search_manual('问题')
        assert caught.value.code == 'unavailable'
        assert 'secret-value' not in str(caught.value) + json.dumps(caught.value.details)


async def test_local_manual_missing_independent_embedding_config_is_typed():
    from backend.services.manual import LocalRAGManualProvider
    class Local:
        def embedding_info(self):
            return {'configured': False, 'missing_fields': ['OWN_EMBEDDING_KEY']}
        async def search_manual(self, *args):
            pytest.fail('Missing embedding credentials must reject before cache/index/search')
    with pytest.raises(ProviderError) as caught:
        await LocalRAGManualProvider(Local()).search_manual('问题')
    assert caught.value.code == 'not_configured'
    assert caught.value.details['missing_fields'] == ['OWN_EMBEDDING_KEY']


async def test_local_manual_invalid_index_is_unavailable_with_diagnostics():
    from backend.services.manual import LocalRAGManualProvider
    class Local:
        def embedding_info(self):
            return {'configured': True}
        def info(self):
            return {'status': 'unavailable'}
        async def search_manual(self, *args):
            raise RuntimeError('Index identity differs from embedding model')
    with pytest.raises(ProviderError) as caught:
        await LocalRAGManualProvider(Local()).search_manual('问题')
    assert caught.value.code == 'unavailable'
    assert caught.value.details['exception_type'] == 'RuntimeError'
