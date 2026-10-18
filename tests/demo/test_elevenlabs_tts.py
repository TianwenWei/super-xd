"""In-memory WS tests: no ElevenLabs credentials, connections or synthesis."""
import asyncio
import base64
import io
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import wave

import pytest
from websockets.exceptions import ConnectionClosedOK

from super_xd.demo.audio.providers import TTSProvider
from super_xd.nlp.contracts.providers import ProviderError
from super_xd.demo.audio.elevenlabs_tts import DEFAULT_VOICE, ElevenLabsTTSProvider


SECRET = 'offline-private-fixture-key'
PCM = b'\1\0' * 24


class Socket:
    def __init__(self, *, chunks=None, block=False, early=False):
        self.messages, self.queue = [], asyncio.Queue()
        self.chunks = [PCM] if chunks is None else chunks
        self.block, self.early = block, early
        self.closed = False
        self.response = SimpleNamespace(headers={'x-region': 'eu-west-fixture', 'x-request-id': 'fixture-123',
                                                 'xi-api-key': SECRET})

    async def send(self, raw):
        message = json.loads(raw)
        self.messages.append(message)
        if message.get('flush') and not self.block:
            for chunk in self.chunks:
                self.queue.put_nowait(chunk if isinstance(chunk, dict) else {
                    'audio': base64.b64encode(chunk).decode(), 'isFinal': False})
            if self.early:
                self.queue.put_nowait({'isFinal': True})
        if message['text'] == '' and not self.block:
            self.queue.put_nowait({'is_final': True})

    async def recv(self):
        item = await self.queue.get()
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item)

    async def close(self):
        self.closed = True
        self.queue.put_nowait(ConnectionClosedOK(None, None))


class Connector:
    def __init__(self, **options):
        self.options, self.calls, self.sockets = options, [], []

    async def __call__(self, url, **options):
        self.calls.append((url, options))
        socket = Socket(**self.options)
        self.sockets.append(socket)
        return socket


@pytest.fixture(autouse=True)
def fixture_key(monkeypatch):
    monkeypatch.setenv('OFFLINE_ELEVEN_KEY', SECRET)


def provider(connect=None, **changes):
    return ElevenLabsTTSProvider({'provider': 'elevenlabs', 'key_env': 'OFFLINE_ELEVEN_KEY',
        'model': 'eleven_flash_v2_5', 'language': 'zh', 'timeout_seconds': 1, **changes},
        connect=connect if connect is not None else Connector())


async def texts(*values):
    for value in values:
        yield value


async def collect(tts, source=None, **options):
    return [event async for event in tts.stream_synthesize(source if source is not None else texts('你好。'), **options)]


async def test_first_clause_flushes_before_later_text_and_contract_keeps_actual_pcm():
    connector = Connector()
    ready, closed = asyncio.Event(), asyncio.Event()

    async def source():
        try:
            yield '好的，'
            await ready.wait()
            yield '我来调整。'
        finally:
            closed.set()

    tts = provider(connector)
    assert isinstance(tts, TTSProvider)
    stream = tts.stream_synthesize(source())
    first = await asyncio.wait_for(anext(stream), .2)
    assert first == {'type': 'audio', 'audio': PCM, 'sample_rate': 24000, 'channels': 1, 'format': 'pcm_s16le'}
    socket = connector.sockets[0]
    assert socket.messages[0]['text'] == ' ' and 'voice_settings' in socket.messages[0]
    assert socket.messages[1] == {'text': '好的， ', 'flush': True}
    assert len(socket.messages) == 2 and not closed.is_set()
    ready.set()
    remaining = [event async for event in stream]
    assert socket.messages[2:] == [{'text': '我来调整。 ', 'flush': True}, {'text': ''}]
    assert remaining[-1]['audio_bytes'] == 96 and remaining[-1]['text_chunks'] == 2
    assert remaining[-1]['response_headers'] == {'x-region': 'eu-west-fixture', 'x-request-id': 'fixture-123'}
    assert remaining[-1]['first_sent_text_to_audio_ms'] >= 0
    assert closed.is_set() and socket.closed
    url, options = connector.calls[0]
    parsed = urlsplit(url)
    assert parsed.scheme == 'wss' and parsed.hostname == 'api.elevenlabs.io'
    assert parsed.path == f'/v1/text-to-speech/{DEFAULT_VOICE}/stream-input'
    assert parse_qs(parsed.query) == {'model_id': ['eleven_flash_v2_5'], 'language_code': ['zh'], 'output_format': ['pcm_24000']}
    assert options['additional_headers'] == {'xi-api-key': SECRET} and SECRET not in url
    assert options['compression'] is None


async def test_one_shot_uses_same_stream_and_wav_encodes_only_returned_pcm():
    connector = Connector()
    result = await provider(connector).synthesize_with_metrics('你好。')
    assert len(connector.sockets) == 1 and connector.sockets[0].closed
    assert result['content_type'] == 'audio/wav'
    assert result['first_packet_ms'] <= result['total_ms']
    with wave.open(io.BytesIO(result['audio'])) as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (24000, 1, 2)
        assert audio.readframes(audio.getnframes()) == PCM


async def test_prewarm_makes_no_connection_or_filler_and_reports_no_reusable_context():
    connector = Connector()
    result = await provider(connector).prewarm_stream()
    assert result['ok'] and result['prewarmed'] is False
    assert result['synthesis_requests'] == 0 and not connector.calls


@pytest.mark.parametrize('voice', [None, '', 'assistant', 'default', '小何2.0', 'xiaohe2.0', 'zh_female_xiaohe_uranus_bigtts'])
async def test_default_alias_uses_current_provider_voice(voice):
    connector = Connector()
    await collect(provider(connector, voices={'default': 'FixtureVoice123'}), voice=voice)
    assert '/FixtureVoice123/' in connector.calls[0][0]


async def test_each_utterance_uses_its_own_explicit_voice_context():
    connector = Connector()
    tts = provider(connector, voices={'warm': 'AnotherFixture123'})
    await collect(tts)
    await collect(tts, voice='warm')
    assert len(connector.sockets) == 2 and all(socket.closed for socket in connector.sockets)
    assert f'/{DEFAULT_VOICE}/' in connector.calls[0][0] and '/AnotherFixture123/' in connector.calls[1][0]
    with pytest.raises(ProviderError) as caught:
        await collect(tts, voice='unconfigured')
    assert caught.value.code == 'unsupported_capability' and len(connector.calls) == 2


@pytest.mark.parametrize('changes', [
    {'base_url': 'https://collector.example'}, {'url_env': 'OTHER_ENDPOINT'},
    {'model': '../invalid'}, {'language': 'zh-CN'}, {'key_env': 'bad key'},
    {'timeout_seconds': 0}, {'timeout_seconds': float('nan')}, {'timeout_seconds': True},
    {'voices': {'assistant': ''}}, {'voices': {'assistant': '../voice'}},
])
def test_invalid_configuration_rejected_before_connection(changes):
    with pytest.raises(ValueError):
        provider(**changes)


async def test_missing_key_and_empty_text_never_open_a_connection(monkeypatch):
    connector = Connector()
    monkeypatch.delenv('OFFLINE_ELEVEN_KEY')
    tts = provider(connector)
    assert not tts.info()['configured']
    with pytest.raises(ProviderError) as caught:
        await tts.prewarm_stream()
    assert caught.value.code == 'not_configured'
    monkeypatch.setenv('OFFLINE_ELEVEN_KEY', SECRET)
    assert await collect(provider(connector), texts('', ' ')) == []
    with pytest.raises(ProviderError):
        await provider(connector).synthesize_with_metrics(' ')
    assert not connector.calls


async def test_alignment_is_preserved_as_diagnostic_without_fabricated_sentence_boundaries():
    alignment = {'chars': ['你', '好'], 'char_start_times_ms': [0, 12], 'char_durations_ms': [12, 15]}
    connector = Connector(chunks=[{'audio': base64.b64encode(PCM).decode(), 'alignment': alignment}])
    tts = provider(connector)
    events = await collect(tts)
    assert [event['type'] for event in events] == ['audio', 'diagnostic', 'metrics']
    assert events[1]['provider_event']['alignment'] == alignment
    assert events[-1]['alignment_events'] == 1
    assert tts.info()['capabilities']['alignment'] is False


@pytest.mark.parametrize('flag', ['isFinal', 'is_final'])
async def test_nullable_completion_flag_on_real_shape_audio_is_not_a_final(flag):
    connector = Connector(chunks=[{'audio': base64.b64encode(PCM).decode(), flag: None,
                                  'alignment': None, 'normalizedAlignment': None}])
    events = await collect(provider(connector), texts('你好，', '我在。', '需要我帮你做什么？'))
    assert events[-1]['type'] == 'metrics'
    assert events[-1]['audio_bytes'] == len(PCM) * 3 and events[-1]['text_chunks'] == 3
    assert [event['audio'] for event in events if event['type'] == 'audio'] == [PCM] * 3
    assert connector.sockets[0].messages[-1] == {'text': ''}


async def test_nullable_audio_followed_by_eof_still_requires_explicit_final():
    class MissingFinal(Socket):
        async def send(self, raw):
            message = json.loads(raw)
            self.messages.append(message)
            if message.get('flush'):
                self.queue.put_nowait({'audio': base64.b64encode(PCM).decode(), 'isFinal': None})
            elif message['text'] == '':
                self.queue.put_nowait(ConnectionClosedOK(None, None))

    socket = MissingFinal()

    async def connect(*args, **kwargs):
        return socket

    seen = []
    with pytest.raises(ProviderError) as caught:
        await collect(provider(connect), on_event=seen.append)
    assert caught.value.code == 'invalid_response'
    assert [event['type'] for event in seen] == ['audio'] and socket.closed


async def test_odd_audio_chunks_reassemble_and_incomplete_or_empty_pcm_fails():
    events = await collect(provider(Connector(chunks=[b'\1', b'\0\2\0'])))
    assert b''.join(event['audio'] for event in events if event['type'] == 'audio') == b'\1\0\2\0'
    for chunks in ([b'\1'], []):
        with pytest.raises(ProviderError) as caught:
            await collect(provider(Connector(chunks=chunks)))
        assert caught.value.code == 'invalid_response'


@pytest.mark.parametrize('payload', [{'audio': '?not-base64'}, {'audio': [1]}, {'is_final': 'yes'}])
async def test_malformed_audio_or_completion_flag_rejected(payload):
    connector = Connector(chunks=[payload])
    with pytest.raises(ProviderError) as caught:
        await collect(provider(connector))
    assert caught.value.code == 'invalid_response' and connector.sockets[0].closed


async def test_wav_is_not_mislabelled_as_raw_pcm_and_response_size_limits_are_enforced(monkeypatch):
    import super_xd.demo.audio.elevenlabs_tts as module
    with pytest.raises(ProviderError) as caught:
        await collect(provider(Connector(chunks=[b'RI', b'FF1234WAVEdata'])))
    assert caught.value.code == 'invalid_response'
    monkeypatch.setattr(module, 'MAX_AUDIO_BYTES', len(PCM) - 2)
    with pytest.raises(ProviderError) as caught:
        await collect(provider())
    assert caught.value.code == 'invalid_response'


async def test_bad_later_input_and_total_text_limit_wake_receiver_immediately(monkeypatch):
    import super_xd.demo.audio.elevenlabs_tts as module
    connector = Connector(block=True)
    with pytest.raises(ProviderError) as caught:
        await asyncio.wait_for(collect(provider(connector), texts('第一段。', None)), .2)
    assert caught.value.code == 'invalid_request' and connector.sockets[0].closed
    monkeypatch.setattr(module, 'MAX_TEXT_BYTES', 8)
    with pytest.raises(ProviderError) as caught:
        await collect(provider(), texts('你好', '再见'))
    assert caught.value.code == 'invalid_request'


async def test_early_final_cancels_blocked_input_without_waiting_for_deadline():
    closed = asyncio.Event()

    async def source():
        try:
            yield '第一段。'
            await asyncio.Event().wait()
        finally:
            closed.set()

    connector = Connector(early=True)
    with pytest.raises(ProviderError) as caught:
        await asyncio.wait_for(collect(provider(connector), source()), .2)
    assert caught.value.code == 'invalid_response' and closed.is_set()
    assert connector.sockets[0].closed


async def test_remote_close_without_final_is_not_success():
    connector = Connector(block=True)
    task = asyncio.create_task(collect(provider(connector)))
    while not connector.sockets:
        await asyncio.sleep(0)
    await connector.sockets[0].close()
    with pytest.raises(ProviderError) as caught:
        await task
    assert caught.value.code == 'invalid_response'


async def test_cancel_and_consumer_close_release_websocket_sender_and_text_source():
    for explicit_cancel in (False, True):
        closed = asyncio.Event()

        async def source():
            try:
                yield '第一段。'
                await asyncio.Event().wait()
            finally:
                closed.set()

        connector = Connector(block=explicit_cancel)
        tts = provider(connector)
        stream = tts.stream_synthesize(source())
        if explicit_cancel:
            task = asyncio.create_task(anext(stream))
            while not connector.sockets or not connector.sockets[0].messages:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await anext(stream)
            await stream.aclose()
        assert closed.is_set() and connector.sockets[0].closed
        assert not tts._sockets and not tts._senders


async def test_cancel_racing_a_noncooperative_handshake_closes_late_socket():
    entered = asyncio.Event()
    socket = Socket()

    async def connect(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return socket

    task = asyncio.create_task(collect(provider(connect)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket.closed


async def test_provider_close_interrupts_first_text_and_active_streams():
    entered, closed = asyncio.Event(), asyncio.Event()

    async def source():
        try:
            entered.set()
            await asyncio.Event().wait()
            yield '不可达'
        finally:
            closed.set()

    tts = provider()
    task = asyncio.create_task(collect(tts, source()))
    await entered.wait()
    await tts.aclose()
    with pytest.raises(ProviderError) as caught:
        await task
    assert caught.value.code == 'unavailable' and closed.is_set()
    await tts.aclose()
    connector = Connector(block=True)
    tts = provider(connector)
    task = asyncio.create_task(collect(tts))
    while not connector.sockets:
        await asyncio.sleep(0)
    await tts.aclose()
    with pytest.raises(ProviderError) as caught:
        await task
    assert caught.value.code == 'unavailable' and connector.sockets[0].closed


async def test_timeout_covers_handshake_and_empty_messages_without_retry():
    calls = 0

    async def blocked(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()

    with pytest.raises(ProviderError) as caught:
        await collect(provider(blocked, timeout_seconds=.02))
    assert caught.value.code == 'timeout' and calls == 1

    class EmptySocket(Socket):
        async def recv(self):
            await asyncio.sleep(.001)
            return '{}'

    socket = EmptySocket()

    async def connect(*args, **kwargs):
        return socket

    with pytest.raises(ProviderError) as caught:
        await collect(provider(connect, timeout_seconds=.02))
    assert caught.value.code == 'timeout' and socket.closed


async def test_deadline_does_not_cancel_downstream_consumer_across_yield():
    connector = Connector()
    stream = provider(connector, timeout_seconds=.02).stream_synthesize(texts('第一段。'))
    await anext(stream)
    await asyncio.sleep(.04)
    with pytest.raises(ProviderError) as caught:
        await anext(stream)
    assert caught.value.code == 'timeout' and connector.sockets[0].closed


@pytest.mark.parametrize('code,expected', [('missing_permissions', 'permission_denied'),
    ('invalid_api_key', 'auth'), ('rate_limit_exceeded', 'rate_limit'), ('quota_exceeded', 'quota_exceeded')])
async def test_provider_error_body_is_typed_and_redacted(code, expected):
    connector = Connector(chunks=[{'detail': {'status': code, 'message': SECRET + ' unknown-private-value'}}])
    with pytest.raises(ProviderError) as caught:
        await collect(provider(connector))
    error = caught.value
    assert error.code == expected and error.details['provider_code'] == code
    public = json.dumps(error.public()) + json.dumps(error.details)
    assert SECRET not in public and 'unknown-private-value' not in public


@pytest.mark.parametrize('cancel', [False, True])
async def test_source_cleanup_error_is_redacted_without_overriding_cancellation(cancel):
    entered = asyncio.Event()

    class Source:
        def __aiter__(self):
            return self

        async def __anext__(self):
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise StopAsyncIteration

        async def aclose(self):
            raise RuntimeError(SECRET + ' unknown-private-value')

    task = asyncio.create_task(collect(provider(), Source()))
    await entered.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(ProviderError) as caught:
            await task
        assert SECRET not in json.dumps(caught.value.details)
        assert 'unknown-private-value' not in str(caught.value)


async def test_callback_gets_normalized_events_and_failure_closes_socket():
    events = []
    await collect(provider(), on_event=events.append)
    assert [event['type'] for event in events] == ['audio', 'metrics']
    connector = Connector()

    async def fail(_):
        raise RuntimeError(SECRET)

    with pytest.raises(ProviderError) as caught:
        await collect(provider(connector), on_event=fail)
    assert SECRET not in str(caught.value) and connector.sockets[0].closed
