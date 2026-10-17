"""Bounded transport tests use local HTTP doubles and real cancellation boundaries."""
import asyncio
import json
import time

import httpx
import pytest

from backend.models import ArkClient
from tests.test_model_trace import Frames, sse


@pytest.fixture(autouse=True)
def credential(monkeypatch):
    monkeypatch.setenv('ARK_API_KEY', 'offline-deadline-fixture')


def final_frames(text='answer', usage=None):
    chunks = [sse({'id': 'success-id', 'choices': [{'delta': {'content': text}, 'finish_reason': 'stop'}]})]
    if usage:
        chunks.append(sse({'choices': [], 'usage': usage}))
    return Frames(chunks + [b'data: [DONE]\n\n'])


async def collect(ark, **options):
    return [item async for item in ark.stream([{'role': 'user', 'content': 'test'}], **options)]


async def test_fast_retry_retains_both_attempts_and_cumulative_timings_without_duplicate_payload():
    calls = []
    usage = {'prompt_tokens': 50, 'completion_tokens': 3, 'prompt_tokens_details': {'cached_tokens': 32}}
    async def respond(request):
        calls.append(request)
        await asyncio.sleep(0.01)
        if len(calls) == 1:
            return httpx.Response(503, json={'error': {'code': 'busy'}}, headers={'x-request-id': 'failed-id'})
        return httpx.Response(200, stream=final_frames(usage=usage), headers={'x-request-id': 'success-http-id'})
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(respond)) as client:
        final = (await collect(ArkClient(client), total_timeout_seconds=1))[-1]
    meta = final['meta']; first, second = meta['attempts']
    assert len(calls) == 2 and first['status'] == 'failed' and first['code'] == 'busy'
    assert first['provider_request_id'] == 'failed-id'
    assert second['status'] == 'completed' and second['provider_request_id'] == 'success-http-id'
    assert second['provider_id'] == 'success-id' and meta['usage'] == usage
    assert meta['first_delta_ms'] == pytest.approx(second['start_offset_ms'] + second['first_delta_ms'])
    assert meta['response_headers_ms'] == pytest.approx(second['start_offset_ms'] + second['response_headers_ms'])
    assert meta['elapsed_ms'] >= first['elapsed_ms'] + second['elapsed_ms']
    assert all('actual_request' not in attempt and 'raw_chunks' not in attempt for attempt in meta['attempts'])
    assert meta['actual_request'] == json.loads(calls[-1].content)
    assert first['started_at'] <= first['ended_at'] <= second['ended_at']


@pytest.mark.parametrize('failure', [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout])
async def test_transport_timeout_is_not_retried(failure):
    calls = []
    def respond(request):
        calls.append(request)
        raise failure('fixture timeout')
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(RuntimeError) as caught:
            await collect(ArkClient(client))
    assert len(calls) == 1 and caught.value.provider_code == 'timeout'
    assert caught.value.retryable is False
    assert caught.value.trace_partial['meta']['attempts'][0]['code'] == 'timeout'


@pytest.mark.parametrize('streaming', [True, False])
async def test_deadline_covers_waiting_for_headers_and_cancels_provider(streaming):
    cancelled = asyncio.Event(); calls = []
    async def respond(request):
        calls.append(request)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(RuntimeError) as caught:
            if streaming:
                await collect(ArkClient(client), first_output_timeout_seconds=0.02, total_timeout_seconds=1)
            else:
                await ArkClient(client).complete([], first_output_timeout_seconds=0.001, total_timeout_seconds=0.02)
    assert cancelled.is_set() and len(calls) == 1 and caught.value.provider_code == 'timeout'
    assert caught.value.trace_partial['meta']['deadline_phase'] == ('first_output' if streaming else 'total')


@pytest.mark.parametrize('delta', [{'reasoning_content': 'thinking'}, {'role': 'assistant'},
                                  {'tool_calls': [{'index': 0, 'function': {'arguments': ''}}]}])
async def test_non_output_chunks_do_not_extend_first_output_deadline(delta):
    class Heartbeat(Frames):
        async def __aiter__(self):
            while True:
                yield sse({'id': 'heartbeat-id', 'choices': [{'delta': delta}]})
                await asyncio.sleep(0.002)
    wire = Heartbeat([])
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=wire))) as client:
        with pytest.raises(RuntimeError) as caught:
            await collect(ArkClient(client), first_output_timeout_seconds=0.02, total_timeout_seconds=1)
    meta = caught.value.trace_partial['meta']
    assert meta['deadline_phase'] == 'first_output' and meta['first_output_ms'] is None
    assert meta['attempts'][0]['provider_id'] == 'heartbeat-id' and wire.closed
    assert len(meta['attempts']) == 1 and meta['raw_chunks']


@pytest.mark.parametrize('part', [{'id': 'call'}, {'function': {'name': 'query'}}, {'function': {'arguments': '{'}}])
async def test_any_meaningful_tool_fragment_satisfies_first_output_deadline(part):
    class ToolOnly(Frames):
        async def __aiter__(self):
            yield sse({'choices': [{'delta': {'tool_calls': [{'index': 0, **part}]}}]})
            await asyncio.sleep(0.03)
            yield sse({'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]})
            yield b'data: [DONE]\n\n'
    wire = ToolOnly([])
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=wire))) as client:
        final = (await collect(ArkClient(client), first_output_timeout_seconds=0.01, total_timeout_seconds=1))[-1]
    assert final['meta']['first_delta_ms'] is None
    assert final['meta']['first_output_ms'] is not None and wire.closed


async def test_total_deadline_still_applies_after_partial_output_and_never_retries():
    wire = Frames([sse({'choices': [{'delta': {'content': 'partial'}}]})], blocked=True)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, stream=wire)
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(RuntimeError) as caught:
            await collect(ArkClient(client), first_output_timeout_seconds=0.01, total_timeout_seconds=0.03)
    assert caught.value.trace_partial['message']['content'] == 'partial'
    assert caught.value.trace_partial['meta']['deadline_phase'] == 'total'
    assert len(calls) == 1 and wire.closed


async def test_retries_share_original_first_output_deadline():
    calls = []
    async def respond(request):
        calls.append(request)
        if len(calls) == 1:
            await asyncio.sleep(0.01)
            return httpx.Response(503)
        await asyncio.Event().wait()
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(RuntimeError) as caught:
            await collect(ArkClient(client), first_output_timeout_seconds=0.04, total_timeout_seconds=0.08)
    meta = caught.value.trace_partial['meta']
    assert len(calls) == 2 and len(meta['attempts']) == 2
    assert meta['deadline_phase'] == 'first_output' and meta['attempts'][1]['code'] == 'timeout'
    assert meta['elapsed_ms'] < 70
    assert meta['elapsed_ms'] >= sum(a['elapsed_ms'] for a in meta['attempts'])


async def test_slow_transient_error_does_not_trigger_another_attempt():
    calls = []
    async def respond(request):
        calls.append(request)
        await asyncio.sleep(0.04)
        return httpx.Response(503)
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(RuntimeError) as caught:
            await collect(ArkClient(client), total_timeout_seconds=0.1)
    assert len(calls) == 1 and caught.value.trace_partial['meta']['attempts'][0]['code'] == 'http_503'


async def test_cancel_after_retry_preserves_attempt_history_and_closes_stream():
    wire = Frames([sse({'choices': [{'delta': {'content': 'partial'}}]})], blocked=True)
    calls = []; got_text = asyncio.Event()
    def respond(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, stream=wire)
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(respond)) as client:
        async def consume():
            async for item in ArkClient(client).stream([]):
                if item['type'] == 'delta': got_text.set()
        task = asyncio.create_task(consume())
        await got_text.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
    meta = caught.value.trace_partial['meta']
    assert [a['status'] for a in meta['attempts']] == ['failed', 'cancelled']
    assert meta['attempts'][1]['code'] == 'cancelled' and wire.closed
    assert caught.value.trace_partial['message']['content'] == 'partial'


async def test_deadline_does_not_cancel_consumer_while_generator_is_yielding():
    wire = final_frames()
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=wire))) as client:
        source = ArkClient(client).stream([], total_timeout_seconds=0.02)
        assert (await anext(source))['type'] == 'delta'
        await asyncio.sleep(0.03)
        with pytest.raises(RuntimeError) as caught:
            await anext(source)
    assert caught.value.provider_code == 'timeout' and wire.closed


async def test_profile_timeout_is_cumulative_stream_fallback():
    wire = Frames([], blocked=True)
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=wire))) as client:
        with pytest.raises(RuntimeError) as caught:
            await collect(ArkClient(client, request_timeout=0.02))
    assert caught.value.trace_partial['meta']['deadline_phase'] == 'total' and wire.closed


async def test_closing_consumer_after_text_closes_upstream_immediately():
    wire = Frames([sse({'choices': [{'delta': {'content': 'partial'}}]})], blocked=True)
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=wire))) as client:
        source = ArkClient(client).stream([])
        await anext(source)
        await source.aclose()
        assert wire.closed


async def test_done_marker_drain_is_still_bounded_by_total_deadline():
    wire = Frames([sse({'choices': [{'delta': {'content': 'answer'}, 'finish_reason': 'stop'}]}),
                   b'data: [DONE]\n\n'], blocked=True)
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=wire))) as client:
        with pytest.raises(RuntimeError) as caught:
            await collect(ArkClient(client), total_timeout_seconds=0.02)
    assert caught.value.trace_partial['meta']['done_received'] is True
    assert caught.value.trace_partial['meta']['deadline_phase'] == 'total' and wire.closed


async def test_enter_cleanup_failure_cannot_replace_original_provider_error():
    class BrokenContext:
        async def __aenter__(self):
            raise httpx.ConnectTimeout('original connect timeout')
        async def __aexit__(self, *args):
            raise RuntimeError('secondary cleanup error')
    class Client:
        def stream(self, *args, **kwargs):
            return BrokenContext()
    with pytest.raises(RuntimeError) as caught:
        await collect(ArkClient(Client()))
    assert caught.value.provider_code == 'timeout'
    assert caught.value.full_diagnostic == 'original connect timeout'
    assert caught.value.trace_partial['meta']['stream_close_error'] == 'secondary cleanup error'


async def test_first_attempt_failure_survives_final_error():
    calls = []
    def respond(request):
        calls.append(request)
        number = len(calls)
        return httpx.Response(503 if number == 1 else 403,
            json={'error': {'code': 'busy' if number == 1 else 'denied'}},
            headers={'x-request-id': f'provider-{number}'})
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(RuntimeError) as caught:
            await collect(ArkClient(client))
    assert caught.value.provider_code == 'denied'
    attempts = caught.value.trace_partial['meta']['attempts']
    assert [a['code'] for a in attempts] == ['busy', 'denied']
    assert [a['provider_request_id'] for a in attempts] == ['provider-1', 'provider-2']


async def test_tool_delta_without_text_prevents_retry_on_disconnection():
    class Disconnect(Frames):
        async def __aiter__(self):
            yield sse({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call'}]}}]})
            raise httpx.ReadError('connection lost after tool output')
    wire = Disconnect([]); calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, stream=wire)
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(RuntimeError) as caught:
            await collect(ArkClient(client))
    assert len(calls) == 1 and wire.closed
    assert caught.value.trace_partial['message']['tool_calls'][0]['id'] == 'call'


@pytest.mark.parametrize('outer_service', [False, True])
async def test_closing_forwarding_layers_closes_http_stream_before_return(outer_service):
    from backend.services.config import ServiceConfig
    from backend.services.contracts import LLMRequest
    from backend.services.llm import ChatProvider, LLMService
    from tests.test_service_config_llm import document

    wire = Frames([sse({'choices': [{'delta': {'content': 'partial'}}]})], blocked=True)
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=wire))) as client:
        provider = ChatProvider({'provider': 'ark', 'key_env': 'ARK_API_KEY'}, transport=ArkClient(client))
        if outer_service:
            config = ServiceConfig(document())
            service = LLMService(config, providers={'ark': provider}, owner=False)
            source = service.stream([])
        else:
            source = provider.stream(LLMRequest([], model='fixture-model'))
        assert (await anext(source))['text'] == 'partial'
        await source.aclose()
        assert wire.closed


async def test_service_info_reports_effective_role_deadlines_without_changing_request_defaults():
    from backend.services.config import ServiceConfig
    from backend.services.llm import LLMService
    from tests.test_service_config_llm import document

    config = document()
    config['llm']['profiles']['ark']['timeout_seconds'] = 37
    config['llm']['roles']['talker'].update(first_output_timeout_seconds=4, total_timeout_seconds=9)
    config['llm']['roles']['thinker'].pop('first_output_timeout_seconds', None)
    config['llm']['roles']['thinker'].pop('total_timeout_seconds', None)
    service = LLMService(ServiceConfig(config))
    try:
        roles = service.info()['roles']
        assert roles['talker']['first_output_timeout_seconds'] == 4
        assert roles['talker']['total_timeout_seconds'] == 9
        assert roles['thinker']['first_output_timeout_seconds'] is None
        assert roles['thinker']['total_timeout_seconds'] == 37
        assert service.request([], role='thinker').total_timeout_seconds is None
    finally:
        await service.aclose()
