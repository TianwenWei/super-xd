"""Provider observation tests use explicit HTTP doubles, never invented live data."""
import asyncio
import json

import httpx
import pytest

from backend.models import ArkClient, sanitized_error


def sse(body):
    return ('data: ' + json.dumps(body, ensure_ascii=False) + '\n\n').encode()


class Frames(httpx.AsyncByteStream):
    def __init__(self, frames, blocked=False):
        self.frames, self.blocked, self.closed = frames, blocked, False

    async def __aiter__(self):
        for frame in self.frames:
            yield frame
        if self.blocked:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv('ARK_API_KEY', 'fake-transport-only-credential')


async def test_full_stream_reassembles_arguments_and_preserves_usage_only_frame():
    chunks = [
        {'id': 'provider1', 'model': 'actual-model', 'created': 123, 'service_tier': 'default', 'choices': [{'delta': {'role': 'assistant', 'reasoning_content': '供应商实际返回'}, 'finish_reason': None}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'tool1', 'type': 'function', 'function': {'name': 'set_navigation_destination', 'arguments': '{"destination": "'}}]}}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '上海虹桥站"}'}}]}, 'finish_reason': 'tool_calls'}]},
        {'choices': [], 'usage': {'prompt_tokens': 51, 'completion_tokens': 12, 'total_tokens': 63,
                                 'prompt_tokens_details': {'cached_tokens': 32}}, 'provider_extension': 'kept'},
    ]
    wire = b''.join(sse(chunk) for chunk in chunks) + b'data: [DONE]\n\n'
    stream = Frames([wire[:147], wire[147:213], wire[213:]])
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, stream=stream)
    async with httpx.AsyncClient(base_url='https://provider.test/', transport=httpx.MockTransport(respond)) as client:
        ark = ArkClient(client)
        records = [item async for item in ark.stream([{'role': 'user', 'content': '导航'}])]
    final = records[-1]
    assert final['message']['content'] is None
    assert final['message']['reasoning_content'] == '供应商实际返回'
    assert json.loads(final['message']['tool_calls'][0]['function']['arguments']) == {'destination': '上海虹桥站'}
    assert final['meta']['finish_reason'] == 'tool_calls'
    assert final['meta']['usage'] == chunks[-1]['usage']
    assert final['meta']['provider_id'] == 'provider1'
    assert final['meta']['raw_chunks'] == chunks
    assert final['meta']['done_received'] is True and stream.closed
    assert requests[0]['stream_options'] == {'include_usage': True}
    assert final['meta']['service_tier'] == 'default'
    assert 0 <= final['meta']['response_headers_ms'] <= final['meta']['first_chunk_ms']
    assert final['meta']['first_chunk_ms'] <= final['meta']['first_tool_delta_ms'] <= final['meta']['elapsed_ms']
    assert final['meta']['first_delta_ms'] is None


async def test_complete_preserves_full_actual_body_and_missing_fields():
    body = {'id': 'real-response-id', 'choices': [{'message': {'role': 'assistant', 'content': None,
            'reasoning_content': 'returned reasoning', 'tool_calls': [], 'extension': {'value': '全文'}}, 'finish_reason': 'stop'}],
            'custom_provider_field': ['保留'], 'service_tier': 'default'}
    def respond(request):
        assert 'stream_options' not in json.loads(request.content)
        return httpx.Response(200, json=body)
    async with httpx.AsyncClient(base_url='https://provider.test/', transport=httpx.MockTransport(respond)) as client:
        response = await ArkClient(client).complete([{'role': 'user', 'content': '测试'}], thinker=True)
    assert response['_meta']['raw_response'] == body
    assert response['_meta']['usage'] is None and response['_meta']['model'] is None
    assert response['_meta']['finish_reason'] == 'stop'
    assert response['content'] is None and response['reasoning_content'] == 'returned reasoning'
    assert response['extension'] == {'value': '全文'}
    assert response['_meta']['service_tier'] == 'default'
    assert 0 <= response['_meta']['response_headers_ms'] <= response['_meta']['elapsed_ms']
    assert response['_meta']['first_tool_delta_ms'] is None


async def test_cancel_preserves_only_observed_partial_and_closes_stream():
    chunk = {'choices': [{'delta': {'content': '已经收到的半句话'}}]}
    stream = Frames([sse(chunk)], blocked=True)
    got_delta = asyncio.Event()
    async with httpx.AsyncClient(base_url='https://provider.test/', transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))) as client:
        async def consume():
            async for item in ArkClient(client).stream([{'role': 'user', 'content': '测试'}]):
                if item['type'] == 'delta':
                    got_delta.set()
        task = asyncio.create_task(consume())
        await got_delta.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
    observed = caught.value.trace_partial
    assert observed['message']['content'] == '已经收到的半句话'
    assert observed['meta']['raw_chunks'] == [chunk]
    assert observed['meta']['usage'] is None and observed['meta']['finish_reason'] is None
    assert observed['meta']['done_received'] is False and stream.closed


async def test_normal_done_drains_http_eof_and_reuses_one_real_tcp_connection():
    """A MockTransport cannot detect a discarded HTTP/1.1 connection."""
    connections, requests, handlers = [], [], set()

    async def serve(reader, writer):
        handler = asyncio.current_task()
        handlers.add(handler)
        connection_id = len(connections) + 1
        connections.append(connection_id)
        try:
            while True:
                headers = await reader.readuntil(b'\r\n\r\n')
                content_length = next(int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n')
                                      if line.lower().startswith(b'content-length:'))
                payload = json.loads(await reader.readexactly(content_length))
                requests.append((connection_id, payload))
                frame = {'id': f'local-{len(requests)}', 'choices': [
                    {'delta': {'content': '本地真实HTTP测试'}, 'finish_reason': 'stop'}]}
                wire = sse(frame) + sse({'choices': [], 'usage': {'prompt_tokens': 12,
                    'completion_tokens': 4, 'total_tokens': 16}}) + b'data: [DONE]\n\n'
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                             b'Transfer-Encoding: chunked\r\nConnection: keep-alive\r\n\r\n')
                writer.write(f'{len(wire):x}\r\n'.encode() + wire + b'\r\n')
                await writer.drain()
                # Send HTTP EndOfMessage separately from the SSE terminal marker.
                # Breaking at [DONE] closes this TCP socket before it is reusable.
                await asyncio.sleep(0.01)
                writer.write(b'0\r\n\r\n')
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            handlers.discard(handler)

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}/', trust_env=False,
                                     timeout=2, http2=False) as client:
            ark = ArkClient(client)
            async with asyncio.timeout(3):
                for _ in range(2):
                    records = [item async for item in ark.stream([{'role': 'user', 'content': '测试'}])]
                    assert records[-1]['meta']['done_received'] is True
                    assert records[-1]['meta']['usage']['total_tokens'] == 16
        assert len(requests) == 2
        assert connections == [1]
        assert [connection for connection, _ in requests] == [1, 1]
        assert all(payload['stream_options'] == {'include_usage': True} for _, payload in requests)
    finally:
        server.close()
        await server.wait_closed()
        pending = tuple(handlers)
        for handler in pending:
            handler.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


async def test_cancel_during_done_eof_drain_closes_without_waiting_for_eof():
    draining = asyncio.Event()
    frame = {'choices': [{'delta': {'content': '已经收到的完整句子。'}, 'finish_reason': 'stop'}]}

    class DoneThenBlocked(Frames):
        async def __aiter__(self):
            yield sse(frame) + b'data: [DONE]\n\n'
            draining.set()
            await asyncio.Event().wait()

    stream = DoneThenBlocked([])
    async with httpx.AsyncClient(base_url='https://provider.test/', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream))) as client:
        async def consume():
            return [item async for item in ArkClient(client).stream([{'role': 'user', 'content': '测试'}])]
        pending = asyncio.create_task(consume())
        async with asyncio.timeout(1):
            await draining.wait()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError) as caught:
                await pending
    assert stream.closed
    assert caught.value.trace_partial['meta']['done_received'] is True
    assert caught.value.trace_partial['message']['content'] == '已经收到的完整句子。'


async def test_malformed_frame_preserves_observed_data_as_error_partial():
    valid = {'choices': [{'delta': {'content': '收到一部分'}}]}
    stream = Frames([sse(valid), b'data: {broken-frame\n\n'])
    async with httpx.AsyncClient(base_url='https://provider.test/', transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))) as client:
        with pytest.raises(RuntimeError) as caught:
            _ = [item async for item in ArkClient(client).stream([{'role': 'user', 'content': '测试'}])]
    assert caught.value.trace_partial['message']['content'] == '收到一部分'
    assert caught.value.trace_partial['meta']['raw_chunks'][-1] == {'unparsed_data': '{broken-frame'}
    assert stream.closed


async def test_complete_keeps_provider_http_error_code_after_header_timing_change():
    stream = Frames([json.dumps({'error': {'code': 'IncorrectAPIKey'}}).encode()])
    async with httpx.AsyncClient(base_url='https://provider.test/', transport=httpx.MockTransport(
            lambda request: httpx.Response(401, stream=stream))) as client:
        with pytest.raises(RuntimeError, match=r'HTTP 401 \(IncorrectAPIKey\)'):
            await ArkClient(client).complete([{'role': 'user', 'content': '测试'}])
    assert stream.closed


def test_http_error_code_cannot_expose_configured_key():
    response = httpx.Response(401, json={'error': {'code': 'fake-transport-only-credential'}}, request=httpx.Request('POST', 'https://example.test'))
    assert 'fake-transport-only-credential' not in sanitized_error(httpx.HTTPStatusError('bad', request=response.request, response=response))


async def test_complete_optional_tool_choice_and_token_budget_do_not_change_default_calls():
    tools = [{'type': 'function', 'function': {'name': 'finish_task', 'parameters': {'type': 'object'}}}]
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'real mocked response'}, 'finish_reason': 'stop'}]})
    async with httpx.AsyncClient(base_url='https://provider.test/', transport=httpx.MockTransport(respond)) as client:
        ark = ArkClient(client)
        await ark.complete([{'role': 'user', 'content': 'default'}], tools=tools)
        forced = {'type': 'function', 'function': {'name': 'finish_task'}}
        await ark.complete([{'role': 'user', 'content': 'bounded finish'}], tools=tools, tool_choice=forced, max_tokens=1200)
        await ark.complete([{'role': 'user', 'content': 'research'}], tools=tools, thinker=True)
    assert requests[0]['tool_choice'] == 'auto' and requests[0]['max_tokens'] == 900
    assert requests[0]['thinking'] == {'type': 'disabled'}
    assert requests[1]['tool_choice'] == forced and requests[1]['max_tokens'] == 1200
    assert requests[1]['model'] == requests[0]['model'] and requests[1]['thinking'] == {'type': 'disabled'}
    assert requests[2]['tool_choice'] == 'auto' and requests[2]['max_tokens'] == 6000
    assert requests[2]['thinking'] == {'type': 'enabled'}
