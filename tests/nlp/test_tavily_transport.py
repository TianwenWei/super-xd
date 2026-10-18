"""Offline transport tests: no Tavily/provider calls or source fabrication."""
from copy import deepcopy
import json

import httpx
import pytest

from super_xd.nlp.adapters.reads.tavily import TavilyClient


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv('TAVILY_API_KEY', 'offline-transport-only-key')


async def test_default_search_keeps_existing_payload_and_full_provider_response():
    provider = {'query': '法国气象局预报', 'results': [
        {'url': 'https://meteofrance.com/previsions-meteo-france/paris/75000', 'content': '完整真实材料由provider返回'}],
        'request_id': 'mock-response', 'response_time': 0.2}
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=provider)

    async with httpx.AsyncClient(base_url='https://tavily.test/', transport=httpx.MockTransport(respond)) as client:
        result = await TavilyClient(client).search('法国气象局预报', 3)
    assert requests == [{'query': '法国气象局预报', 'max_results': 3, 'search_depth': 'basic',
                         'include_answer': False, 'include_raw_content': False}]
    assert {key: value for key, value in result.items() if key != 'queried_at'} == provider
    assert result['queried_at'] and 'request_filters' not in result


async def test_explicit_host_filter_is_sent_to_provider_and_recorded_without_changing_materials():
    domains = ['MeteoFrance.COM', 'meteofrance.fr']
    before = deepcopy(domains)
    provider = {'results': [{'url': 'https://meteofrance.com/forecast', 'content': '完整材料'},
                            {'url': 'https://other.test/returned-by-double', 'content': '不能据请求字段冒充已核实'}],
                'provider_extension': {'kept': True}}
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=provider)

    async with httpx.AsyncClient(base_url='https://tavily.test/', transport=httpx.MockTransport(respond)) as client:
        result = await TavilyClient(client).search('巴黎明天天气', include_domains=domains)
    expected = {'include_domains': ['meteofrance.com', 'meteofrance.fr'], 'include_domains_mode': 'restrict'}
    assert {key: requests[0][key] for key in expected} == expected
    assert requests[0]['query'] == '巴黎明天天气' and requests[0]['max_results'] == 5
    assert result['request_filters'] == expected
    assert result['results'] == provider['results'] and result['provider_extension'] == provider['provider_extension']
    assert domains == before


@pytest.mark.parametrize('domains', [
    [], 'meteofrance.com', ('meteofrance.com',), [''], [' '], [None], [1], [True], [{}],
    ['https://meteofrance.com'], ['meteofrance.com/path'], ['meteofrance.com/'],
    ['meteofrance.com:443'], ['meteofrance.com?query=1'], ['*.meteofrance.com'],
    [' meteofrance.com'], ['meteofrance.com '], ['bad_host.com'], ['-bad.com'], ['bad-.com'],
    ['localhost'], ['127.0.0.1'], ['a..com'], ['meteofrance.com.'], ['法国气象局.com'],
    ['a' * 64 + '.com'], ['meteofrance.com'] * 11, ['meteofrance.com', ''],
])
async def test_invalid_host_list_is_rejected_before_any_http_request(domains):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'results': []})

    async with httpx.AsyncClient(base_url='https://tavily.test/', transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match='include_domains'):
            await TavilyClient(client).search('查询', include_domains=domains)
    assert requests == []
