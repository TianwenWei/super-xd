"""HTTP phase diagnostics must survive failure without retaining wire objects."""
import asyncio
import json
import time

import httpx
import pytest

from backend.models import ArkClient, _MAX_TRANSPORT_EVENTS, _TransportTrace
from tests.test_model_trace import Frames, sse


@pytest.fixture(autouse=True)
def credential(monkeypatch):
    monkeypatch.setenv('ARK_API_KEY', 'offline-transport-trace-key')


async def emit(request, name):
    # httpcore passes headers, exceptions and network streams through this data.
    # None of them may become diagnostic output, even under an accepted name.
    await request.extensions['trace'](name, {
        'headers': {'authorization': 'Bearer fixture-private-token'},
        'return_value': object(), 'exception': RuntimeError('fixture-private-token'),
    })


async def phases(request, protocol='http11'):
    for phase in ('connection.connect_tcp', 'connection.start_tls',
                  f'{protocol}.send_request_headers', f'{protocol}.send_request_body'):
        await emit(request, phase + '.started')
        await emit(request, phase + '.complete')
    await emit(request, f'{protocol}.receive_response_headers.started')


async def call(ark, streaming, **options):
    if streaming:
        return ([item async for item in ark.stream([], **options)])[-1]['meta']
    return (await ark.complete([], **options))['_meta']


@pytest.mark.parametrize('streaming', [True, False])
@pytest.mark.parametrize('cancel', [True, False])
async def test_header_wait_phase_survives_deadline_or_cancellation_and_cleanup(streaming, cancel):
    entered = asyncio.Event()
    async def respond(request):
        await phases(request)
        entered.set()
        try:
            await asyncio.Event().wait()
        except BaseException:
            await emit(request, 'http11.receive_response_headers.failed')
            # Cleanup must not replace the phase which actually failed.
            await emit(request, 'http11.response_closed.started')
            await emit(request, 'http11.response_closed.complete')
            raise
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(respond)) as client:
        options = {'total_timeout_seconds': 1 if cancel else 0.03}
        if streaming and not cancel:
            options['first_output_timeout_seconds'] = 0.02
        task = asyncio.create_task(call(ArkClient(client), streaming, **options))
        await entered.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError) as caught:
            await task
    meta = caught.value.trace_partial['meta']
    diagnostic = meta['transport']
    assert diagnostic['failed_phase'] == 'http11.receive_response_headers'
    assert diagnostic['last_event'] == 'http11.response_closed.complete'
    assert diagnostic['active_phase'] is None and diagnostic['http_version'] is None
    assert diagnostic['dropped_events'] == 0
    assert 'fixture-private-token' not in json.dumps(diagnostic)
    if streaming:
        assert len(meta['attempts']) == 1
        assert meta['attempts'][0]['transport'] == diagnostic


@pytest.mark.parametrize('streaming', [True, False])
async def test_success_records_protocol_and_only_allowlisted_timings(streaming):
    requests = []
    async def respond(request):
        requests.append(request)
        await phases(request, 'http2')
        await emit(request, 'http2.receive_response_headers.complete')
        await emit(request, 'secret.fixture-private-token.started')
        body = {'choices': [{'message': {'role': 'assistant', 'content': 'answer'}, 'finish_reason': 'stop'}]}
        if streaming:
            wire = Frames([sse({'choices': [{'delta': {'content': 'answer'}, 'finish_reason': 'stop'}]}),
                           b'data: [DONE]\n\n'])
            return httpx.Response(200, stream=wire, extensions={'http_version': b'HTTP/2'})
        return httpx.Response(200, json=body, extensions={'http_version': b'HTTP/2'})
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(respond)) as client:
        meta = await call(ArkClient(client), streaming)
    diagnostic = meta['transport']
    assert diagnostic['http_version'] == 'HTTP/2'
    assert diagnostic['failed_phase'] is None and diagnostic['active_phase'] is None
    assert [e['elapsed_ms'] for e in diagnostic['events']] == sorted(e['elapsed_ms'] for e in diagnostic['events'])
    assert all(set(event) == {'phase', 'elapsed_ms'} for event in diagnostic['events'])
    assert 'fixture-private-token' not in json.dumps(diagnostic)
    assert 'extensions' not in json.loads(requests[0].content)


async def test_quick_retry_preserves_independent_transport_failure_before_success():
    count = 0
    async def respond(request):
        nonlocal count
        count += 1
        await phases(request)
        if count == 1:
            await emit(request, 'http11.receive_response_headers.failed')
            raise httpx.ReadError('upstream disconnected')
        await emit(request, 'http11.receive_response_headers.complete')
        return httpx.Response(200, stream=Frames([
            sse({'choices': [{'delta': {'content': 'answer'}, 'finish_reason': 'stop'}]}),
            b'data: [DONE]\n\n']))
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(respond)) as client:
        meta = await call(ArkClient(client), True)
    first, second = meta['attempts']
    assert count == 2
    assert first['transport']['failed_phase'] == 'http11.receive_response_headers'
    assert second['transport']['failed_phase'] is None
    assert second['transport']['http_version'] == 'HTTP/1.1'
    assert first['transport']['http_version'] is None
    assert meta['transport'] == second['transport']


async def test_bounded_events_keep_failure_summary_and_do_not_inspect_callback_info():
    class PrivateObject:
        def __str__(self):
            raise AssertionError('transport diagnostics inspected a private object')
        def __repr__(self):
            raise AssertionError('transport diagnostics inspected a private object')
    trace = _TransportTrace(time.perf_counter())
    private = PrivateObject()
    await trace(['invalid-name'], private)
    await trace('connection.private-secret.started', private)
    for _ in range(_MAX_TRANSPORT_EVENTS):
        await trace('http2.receive_remote_settings.started', private)
        await trace('http2.receive_remote_settings.complete', private)
    await trace('http2.receive_response_headers.started', private)
    active = trace.snapshot()
    assert active['active_phase'] == 'http2.receive_response_headers'
    assert active['active_phase_elapsed_ms'] >= 0
    await trace('http2.receive_response_headers.failed', private)
    await trace('http2.response_closed.started', private)
    await trace('http2.response_closed.complete', private)
    final = trace.snapshot()
    assert len(final['events']) == _MAX_TRANSPORT_EVENTS
    assert final['dropped_events'] == _MAX_TRANSPORT_EVENTS + 4
    assert final['failed_phase'] == 'http2.receive_response_headers'
    assert final['last_event'] == 'http2.response_closed.complete'
    assert final['active_phase'] is None
    assert 'private' not in json.dumps(final)


async def test_transport_without_trace_support_is_explicitly_unobserved():
    async with httpx.AsyncClient(base_url='https://fixture.test/', transport=httpx.MockTransport(
            lambda request: httpx.Response(403, json={'error': {'code': 'denied'}}))) as client:
        with pytest.raises(RuntimeError) as caught:
            await call(ArkClient(client), True)
    diagnostic = caught.value.trace_partial['meta']['transport']
    assert diagnostic['events'] == []
    assert diagnostic['failed_phase'] is None
    assert diagnostic['http_version'] == 'HTTP/1.1'
