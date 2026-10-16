"""Explicit transport and model doubles verify foreground search boundaries."""
import asyncio
import copy
import json

import httpx
import pytest

from backend.db import Database
from backend.models import TavilyClient
from backend.session import SessionActor
from backend.web_search import WebSearchClient
from test_session import FakeArk, call, wait_for


SEARCH = {'query': 'example query', 'results': [
    {'title': 'Source', 'url': 'https://example.com/reference', 'content': 'Search evidence.'}],
    'queried_at': '2026-10-15T12:00:00+00:00'}


async def test_search_uses_real_transport_contract_and_keeps_source_evidence(monkeypatch):
    monkeypatch.setenv('TAVILY_API_KEY', 'offline-test-secret')
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'results': SEARCH['results']})
    async with httpx.AsyncClient(base_url='https://api.tavily.com/', transport=httpx.MockTransport(handler)) as http:
        result = await WebSearchClient(TavilyClient(http)).web_search('example query', include_domains=['example.com'])
    body = json.loads(requests[0].content)
    assert body['query'] == 'example query' and body['max_results'] == 3
    assert body['search_depth'] == 'basic' and body['include_raw_content'] is False
    assert body['include_domains'] == ['example.com']
    assert requests[0].headers['Authorization'] == 'Bearer offline-test-secret'
    assert 'offline-test-secret' not in str(requests[0].url)
    assert result['results'] == SEARCH['results'] and result['queried_at']
    assert result['kind'] == 'web_search' and result['evidence'] == 'search_snippets'


async def test_missing_key_does_not_make_a_network_request(monkeypatch):
    monkeypatch.delenv('TAVILY_API_KEY', raising=False)
    def unexpected(_):
        raise AssertionError('Missing credentials must stop before network IO')
    async with httpx.AsyncClient(base_url='https://api.tavily.com/', transport=httpx.MockTransport(unexpected)) as http:
        with pytest.raises(RuntimeError) as caught:
            await WebSearchClient(TavilyClient(http)).web_search('example query')
    assert 'TAVILY_API_KEY' in str(caught.value)


@pytest.mark.parametrize('response', [httpx.Response(403, json={'error': 'offline-test-secret'}),
                                    httpx.Response(200, json={'results': None})])
async def test_provider_failures_never_become_success(monkeypatch, response):
    monkeypatch.setenv('TAVILY_API_KEY', 'offline-test-secret')
    async with httpx.AsyncClient(base_url='https://api.tavily.com/', transport=httpx.MockTransport(lambda _: response)) as http:
        with pytest.raises(RuntimeError) as caught:
            await WebSearchClient(TavilyClient(http)).web_search('example query')
    assert 'offline-test-secret' not in str(caught.value)


class FakeWeb:
    def __init__(self, *, wait=False, late=False, error=None):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.wait, self.late, self.error = wait, late, error
        self.cancelled = False
        self.requests = []

    async def web_search(self, **arguments):
        self.requests.append(arguments)
        self.started.set()
        if self.wait:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                if not self.late:
                    raise
        if self.error:
            raise self.error
        return {**copy.deepcopy(SEARCH), 'ok': True, 'kind': 'web_search', 'provider': 'Tavily'}


@pytest.fixture
async def make_web_actor(tmp_path):
    actors = []
    async def make(responses, web=None):
        ark, client = FakeArk(responses), web or FakeWeb()
        actor = SessionActor(str(len(actors)), Database(tmp_path / f'{len(actors)}.sqlite'), ark, web=client)
        actors.append(actor)
        queue = await actor.attach()
        await queue.get()
        return actor, queue, ark, client
    yield make
    await asyncio.gather(*(actor.close() for actor in actors))


async def test_search_continues_talker_with_sources_without_creating_task(make_web_actor):
    actor, queue, ark, web = await make_web_actor([
        {'content': '我去查一下。', 'tool_calls': [call('web_search', {'query': 'example query'})]},
        {'content': '查到的摘要是 Search evidence。'}])
    await actor.post({'type': 'input', 'text': '查个实时事实'})
    done, events = await wait_for(queue, 'stream.done')
    assert done['metrics']['route'] == 'lookup' and len(ark.requests) == 2
    assert web.requests == [{'query': 'example query'}] and actor.db.list_tasks(actor.id) == []
    record = next(event for event in events if event['type'] == 'lookup.result')
    assert record['result']['results'] == SEARCH['results']
    assert actor.snapshot()['turns'][0]['lookup_results'][0]['tool'] == 'web_search'
    tool = next(message for message in reversed(ark.requests[1]['messages']) if message['role'] == 'tool')
    assert json.loads(tool['content'])['results'] == SEARCH['results']
    assert actor.recent_places == []
    await actor.trace_queue.join()
    traces = actor.db.traces_after(actor.id, 0, limit=200)
    assert any(event['kind'] == 'tool.completed' and event['data']['name'] == 'web_search' for event in traces)


@pytest.mark.parametrize('arguments', [{'query': ''}, {'query': 'example query', 'max_results': 6},
                                     {'query': 'example query', 'unexpected': True}])
async def test_invalid_search_arguments_do_not_reach_provider(make_web_actor, arguments):
    actor, queue, _, web = await make_web_actor([
        {'tool_calls': [call('web_search', arguments)]}, {'content': '请补充查询内容。'}])
    await actor.post({'type': 'input', 'text': '查一下'})
    await wait_for(queue, 'stream.done')
    assert not web.requests and actor.db.list_tasks(actor.id) == []


async def test_search_timeout_is_an_explicit_error(make_web_actor):
    actor, queue, _, web = await make_web_actor([
        {'tool_calls': [call('web_search', {'query': 'example query'})]}, {'content': '这次查询超时了。'}], FakeWeb(wait=True))
    actor.lookup_timeout = .01
    await actor.post({'type': 'input', 'text': '查一下'})
    _, events = await wait_for(queue, 'stream.done')
    result = next(event['result'] for event in events if event['type'] == 'lookup.result')
    assert result['ok'] is False and result['code'] == 'timeout' and web.cancelled


async def test_search_configuration_error_is_visible(make_web_actor):
    actor, queue, _, _ = await make_web_actor([
        {'tool_calls': [call('web_search', {'query': 'example query'})]}, {'content': '搜索没有配置。'}],
        FakeWeb(error=RuntimeError('TAVILY_API_KEY 未配置，无法查询实时信息。')))
    await actor.post({'type': 'input', 'text': '查一下'})
    _, events = await wait_for(queue, 'stream.done')
    result = next(event['result'] for event in events if event['type'] == 'lookup.result')
    assert result['ok'] is False and 'TAVILY_API_KEY' in result['reason']


async def test_interrupted_search_result_is_trace_only(make_web_actor):
    actor, queue, ark, web = await make_web_actor([
        {'tool_calls': [call('web_search', {'query': 'old query'})]}, {'content': '你好。'}], FakeWeb(wait=True, late=True))
    await actor.post({'type': 'input', 'text': '查一下'})
    await web.started.wait()
    old = actor.current
    await actor.post({'type': 'input', 'text': '算了，先聊天'})
    done, events = await wait_for(queue, 'stream.done')
    await actor.queue.join()
    await actor.trace_queue.join()
    assert done['text'] == '你好。' and len(ark.requests) == 2 and web.cancelled
    assert old.lookup_results == [] and not any(event['type'] == 'lookup.result' for event in events)
    traces = actor.db.traces_after(actor.id, 0, limit=200)
    assert any(event['kind'] == 'tool.completed' and event['status'] == 'stale' for event in traces)
