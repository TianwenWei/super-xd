"""Offline protocol fixtures; expected strings are not real provider recognition."""
import asyncio
import base64
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from super_xd.demo.audio.providers import ASRProvider, ASRStream, PCM16K_ASR
from super_xd.nlp.contracts.providers import ProviderError
from super_xd.demo.audio.elevenlabs_asr import ElevenLabsASRProvider

KEY = 'test-key-never-in-diagnostics'


class Socket:
    def __init__(self, *, commits=None, initial=None):
        self.events = asyncio.Queue()
        self.events.put_nowait(initial if initial is not None else {
            'message_type': 'session_started', 'config': {'audio_format': 'pcm_16000', 'sample_rate': 16000}})
        self.sent = []
        self.commit_requested = asyncio.Event()
        self.read_started = asyncio.Event()
        self.send_started = asyncio.Event()
        self.closed = False
        self.commits = iter(commits or [])
        self.block_send = False
        self.error_send = None

    async def recv(self):
        self.read_started.set()
        event = await self.events.get()
        if isinstance(event, Exception):
            raise event
        return event if isinstance(event, (str, bytes)) else json.dumps(event)

    async def send(self, raw):
        self.send_started.set()
        if self.error_send:
            raise self.error_send
        if self.block_send:
            await asyncio.Event().wait()
        message = json.loads(raw)
        self.sent.append(message)
        if message['commit']:
            self.commit_requested.set()
            value = next(self.commits, None)
            if value is not None:
                self.events.put_nowait({'message_type': 'committed_transcript', 'text': value})

    async def close(self):
        self.closed = True


class Connector:
    def __init__(self, socket):
        self.socket = socket
        self.calls = []

    async def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.socket


def provider(monkeypatch, socket=None, **options):
    monkeypatch.setenv('ELEVEN_TEST_KEY', KEY)
    socket = socket or Socket()
    connector = Connector(socket)
    service = ElevenLabsASRProvider({'key_env': 'ELEVEN_TEST_KEY', **options}, ws_connect=connector)
    return service, socket, connector


async def collect(stream):
    return [item async for item in stream.results()]


async def test_configuration_handshake_and_finish_only_accepts_real_committed(monkeypatch):
    service, socket, connector = provider(monkeypatch)
    assert isinstance(service, ASRProvider) and service.input_format == PCM16K_ASR
    stream = await service.open('capture-1')
    assert isinstance(stream, ASRStream)
    url, options = connector.calls[0]
    assert urlsplit(url).netloc == 'api.elevenlabs.io'
    assert parse_qs(urlsplit(url).query) == {'model_id': ['scribe_v2_realtime'],
        'audio_format': ['pcm_16000'], 'language_code': ['zh'], 'commit_strategy': ['manual']}
    assert options['additional_headers'] == {'xi-api-key': KEY}
    assert options['max_queue'] == 16
    iterator = stream.results()
    await stream.send_audio(b'\x01\x00' * 512)
    socket.events.put_nowait({'message_type': 'partial_transcript', 'text': '中间版本'})
    first = await anext(iterator)
    assert first['type'] == 'partial' and first['text'] == '中间版本'
    finishing = asyncio.create_task(stream.finish())
    await socket.commit_requested.wait()
    assert not finishing.done()
    assert socket.sent[-1] == {'message_type': 'input_audio_chunk', 'audio_base_64': '',
                              'commit': True, 'sample_rate': 16000}
    socket.events.put_nowait({'message_type': 'committed_transcript', 'text': '稳定结果'})
    await finishing
    await stream.finish()
    final = await anext(iterator)
    assert final['type'] == 'final' and final['text'] == '稳定结果'
    assert final['metrics']['audio_bytes'] == 1024
    assert final['metrics']['stop_to_final_ms'] >= 0
    assert final['metrics']['committed_segments'] == 1
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)
    assert socket.closed and not service._connections
    assert KEY not in json.dumps(service.info()) + json.dumps(final)


async def test_committed_prefix_is_preserved_and_not_a_whole_utterance_final(monkeypatch):
    service, socket, _ = provider(monkeypatch, Socket(commits=['第一段', '第三段']))
    stream = await service.open('capture')
    iterator = stream.results()
    for _ in range(100):
        await stream.send_audio(b'\0\0' * 3200)
    item = await anext(iterator)
    assert item['type'] == 'partial' and item['text'] == '第一段'
    await stream.send_audio(b'\0\0')
    socket.events.put_nowait({'message_type': 'partial_transcript', 'text': '暂时'})
    assert (await anext(iterator))['text'] == '第一段暂时'
    await stream.finish()
    final = await anext(iterator)
    assert final['type'] == 'final' and final['text'] == '第一段第三段'
    await iterator.aclose()


async def test_unsolicited_or_stale_committed_segment_fails_instead_of_truncated_success(monkeypatch):
    service, socket, _ = provider(monkeypatch)
    stream = await service.open('unsolicited')
    await stream.send_audio(b'\0\0')
    socket.events.put_nowait({'message_type': 'committed_transcript', 'text': '不是整轮结果'})
    with pytest.raises(ProviderError) as caught:
        await collect(stream)
    assert caught.value.code == 'invalid_response'
    assert stream._final is None and stream._committed == '' and socket.closed


async def test_sixty_seconds_splits_by_exact_audio_bytes_and_concatenates_all_segments(monkeypatch):
    service, socket, _ = provider(monkeypatch, Socket(commits=['段一', '段二', '段三']))
    stream = await service.open('long')
    pcm = b'\x01\x00' * 3200
    for _ in range(300):
        await stream.send_audio(pcm)
    await stream.finish()
    events = await collect(stream)
    assert events[-1]['type'] == 'final' and events[-1]['text'] == '段一段二段三'
    assert events[-1]['metrics']['audio_ms'] == 60000
    assert sum(item['commit'] for item in socket.sent) == 3  # No empty trailing segment.
    counts, count = [], 0
    for item in socket.sent:
        count += len(base64.b64decode(item['audio_base_64']))
        if item['commit']:
            counts.append(count)
            count = 0
    assert counts == [640000] * 3 and count == 0


async def test_finish_waits_for_inflight_segment_commit_without_duplicate_or_deadlock(monkeypatch):
    service, socket, _ = provider(monkeypatch)
    stream = await service.open('parallel')
    for _ in range(99):
        await stream.send_audio(b'\0\0' * 3200)
    send = asyncio.create_task(stream.send_audio(b'\0\0' * 3200))
    await socket.commit_requested.wait()
    finish = asyncio.create_task(stream.finish())
    socket.events.put_nowait({'message_type': 'committed_transcript', 'text': '长段'})
    await asyncio.wait_for(asyncio.gather(send, finish), 1)
    assert sum(item['commit'] for item in socket.sent) == 1
    assert (await collect(stream))[-1]['text'] == '长段'


@pytest.mark.parametrize('pcm', [b'', b'\0', b'\0\0' * 3201, 'pcm', None])
async def test_strict_pcm_frames_reject_before_any_send(monkeypatch, pcm):
    service, socket, _ = provider(monkeypatch)
    stream = await service.open('invalid')
    with pytest.raises(ProviderError) as caught:
        await stream.send_audio(pcm)
    assert caught.value.code == 'invalid_request' and not socket.sent and socket.closed


async def test_empty_audio_is_rejected_but_short_real_audio_is_committed_without_padding(monkeypatch):
    service, socket, _ = provider(monkeypatch)
    stream = await service.open('empty')
    with pytest.raises(ProviderError) as caught:
        await stream.finish()
    assert caught.value.code == 'invalid_request' and not socket.sent
    service, socket, _ = provider(monkeypatch, Socket(commits=['']))
    stream = await service.open('short')
    await stream.send_audio(b'\x01\x00')
    await stream.finish()
    final = (await collect(stream))[-1]
    assert final['text'] == '' and final['metrics']['audio_bytes'] == 2
    assert b''.join(base64.b64decode(item['audio_base_64']) for item in socket.sent) == b'\x01\x00'


@pytest.mark.parametrize('kind, code', [('auth_error', 'auth'), ('quota_exceeded', 'rate_limit'),
    ('insufficient_audio_activity', 'invalid_request'), ('transcriber_error', 'unavailable')])
async def test_provider_errors_are_neutral_redacted_and_not_transcripts(monkeypatch, kind, code):
    service, socket, _ = provider(monkeypatch)
    stream = await service.open('error')
    job = asyncio.create_task(collect(stream))
    socket.events.put_nowait({'message_type': kind, 'error': KEY, 'message': KEY})
    with pytest.raises(ProviderError) as caught:
        await job
    assert caught.value.code == code
    assert KEY not in str(caught.value) + json.dumps(caught.value.details)
    assert socket.closed and not service._connections


@pytest.mark.parametrize('raw', ['broken', '[]', '{}', b'binary',
    json.dumps({'message_type': 'partial_transcript', 'text': 1}),
    json.dumps({'message_type': 'committed_transcript', 'text': '字' * 4001})])
async def test_invalid_responses_never_become_text(monkeypatch, raw):
    service, socket, _ = provider(monkeypatch)
    stream = await service.open('bad')
    socket.events.put_nowait(raw)
    with pytest.raises(ProviderError) as caught:
        await collect(stream)
    assert caught.value.code == 'invalid_response' and socket.closed


async def test_missing_credentials_and_unsupported_model_do_not_connect(monkeypatch):
    monkeypatch.delenv('MISSING_ELEVEN_KEY', raising=False)
    connector = Connector(Socket())
    service = ElevenLabsASRProvider({'key_env': 'MISSING_ELEVEN_KEY'}, ws_connect=connector)
    assert not service.info()['configured']
    with pytest.raises(ProviderError) as caught:
        await service.open('missing')
    assert caught.value.code == 'not_configured' and not connector.calls
    for values in ({'model': 'unknown'}, {'language': 'not-a-code'},
                   {'base_url': 'wss://other.example'}, {'url': 'wss://other.example'}, {'max_record_seconds': 61}):
        with pytest.raises(ValueError):
            ElevenLabsASRProvider(values)


async def test_early_close_preserves_no_fake_final_and_hides_reason(monkeypatch):
    service, socket, _ = provider(monkeypatch)
    stream = await service.open('early')
    iterator = stream.results()
    socket.events.put_nowait({'message_type': 'partial_transcript', 'text': '未确定'})
    assert (await anext(iterator))['text'] == '未确定'
    socket.events.put_nowait(RuntimeError(KEY))
    with pytest.raises(ProviderError) as caught:
        await anext(iterator)
    assert caught.value.code == 'unavailable' and KEY not in str(caught.value)
    assert socket.closed


async def test_finish_timeout_does_not_promote_partial_to_final(monkeypatch):
    service, socket, _ = provider(monkeypatch, timeout_seconds=0.02)
    stream = await service.open('timeout')
    await stream.send_audio(b'\0\0')
    socket.events.put_nowait({'message_type': 'partial_transcript', 'text': '不确定'})
    with pytest.raises(ProviderError) as caught:
        await stream.finish()
    assert caught.value.code == 'timeout' and socket.closed
    with pytest.raises(ProviderError):
        await collect(stream)


@pytest.mark.parametrize('boundary', ['results', 'finish', 'send'])
async def test_cancellation_closes_upstream_at_each_blocked_boundary(monkeypatch, boundary):
    service, socket, _ = provider(monkeypatch)
    stream = await service.open('cancel')
    if boundary == 'results':
        consumer_started = asyncio.Event()
        async def consume():
            consumer_started.set()
            return await collect(stream)
        job = asyncio.create_task(consume())
        await consumer_started.wait()
    elif boundary == 'finish':
        await stream.send_audio(b'\0\0')
        job = asyncio.create_task(stream.finish())
        await socket.commit_requested.wait()
    else:
        socket.block_send = True
        job = asyncio.create_task(stream.send_audio(b'\0\0'))
        await socket.send_started.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert socket.closed and not service._connections
    socket.events.put_nowait({'message_type': 'committed_transcript', 'text': '迟到不得提交'})
    assert stream._final is None


async def test_provider_close_unblocks_result_consumer_and_only_its_own_connections(monkeypatch):
    service, socket, _ = provider(monkeypatch)
    stream = await service.open('closed')
    job = asyncio.create_task(collect(stream))
    await socket.read_started.wait()
    await service.aclose()
    assert await asyncio.wait_for(job, 1) == []
    assert socket.closed and not service._connections


async def test_cancel_connect_closes_late_returned_socket(monkeypatch):
    entered = asyncio.Event()
    socket = Socket()
    async def connect(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return socket  # A non-cooperative adapter may complete during cancellation.
    monkeypatch.setenv('ELEVEN_TEST_KEY', KEY)
    service = ElevenLabsASRProvider({'key_env': 'ELEVEN_TEST_KEY'}, ws_connect=connect)
    job = asyncio.create_task(service.open('cancel-open'))
    await entered.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert socket.closed and not service._connections


@pytest.mark.parametrize('status, code', [(401, 'auth'), (403, 'auth'), (429, 'rate_limit'), (503, 'unavailable')])
async def test_handshake_status_errors_are_neutral_and_never_echo_headers(monkeypatch, status, code):
    class HandshakeError(Exception):
        response = type('Response', (), {'status_code': status})()
    async def connect(*args, **kwargs):
        raise HandshakeError(KEY)
    monkeypatch.setenv('ELEVEN_TEST_KEY', KEY)
    service = ElevenLabsASRProvider({'key_env': 'ELEVEN_TEST_KEY'}, ws_connect=connect)
    with pytest.raises(ProviderError) as caught:
        await service.open('handshake')
    assert caught.value.code == code and caught.value.details['status_code'] == status
    assert KEY not in str(caught.value) + json.dumps(caught.value.details)
    assert not service._connections


async def test_handshake_checks_echoed_wire_format_before_accepting_audio(monkeypatch):
    service, socket, _ = provider(monkeypatch, Socket(initial={
        'message_type': 'session_started', 'config': {'sample_rate': 24000}}))
    with pytest.raises(ProviderError) as caught:
        await service.open('format')
    assert caught.value.code == 'invalid_response' and socket.closed
    assert not socket.sent and not service._connections


async def test_input_budget_is_explicit_and_never_truncates_or_commits_overflow(monkeypatch):
    service, socket, _ = provider(monkeypatch, max_record_seconds=0.1)
    stream = await service.open('budget')
    await stream.send_audio(b'\0\0' * 1600)
    with pytest.raises(ProviderError) as caught:
        await stream.send_audio(b'\0\0')
    assert caught.value.code == 'invalid_request'
    assert stream.metrics['audio_bytes'] == 3200
    assert sum(item['commit'] for item in socket.sent) == 0
    assert socket.closed


async def test_send_timeout_and_close_failure_remain_redacted(monkeypatch):
    service, socket, _ = provider(monkeypatch, timeout_seconds=0.02)
    async def bad_close():
        socket.closed = True
        raise RuntimeError(KEY)
    socket.close = bad_close
    stream = await service.open('send-timeout')
    socket.block_send = True
    with pytest.raises(ProviderError) as caught:
        await stream.send_audio(b'\0\0')
    assert caught.value.code == 'timeout'
    assert KEY not in str(caught.value) + json.dumps(caught.value.details)
    assert socket.closed and not service._connections


async def test_slow_consumer_coalesces_interims_without_losing_stable_segments(monkeypatch):
    service, socket, _ = provider(monkeypatch, Socket(commits=['前段', '结尾']))
    stream = await service.open('slow')
    for _ in range(100):
        await stream.send_audio(b'\0\0' * 3200)
    await stream.send_audio(b'\0\0')
    for i in range(200):
        socket.events.put_nowait({'message_type': 'partial_transcript', 'text': str(i)})
    # Finish waits on the actual reader, with no results consumer needed for progress.
    await stream.finish()
    result = await collect(stream)
    assert result == [{'type': 'final', 'text': '前段结尾', 'metrics': dict(stream.metrics)}]
    assert stream.metrics['committed_segments'] == 2
