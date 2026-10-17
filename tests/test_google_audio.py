"""Google TTS contract tests use an in-memory duplex transport, never synthesis."""
import asyncio
import io
import json
from types import SimpleNamespace
import wave

import pytest

from backend.services.audio import TTSProvider
from backend.services.contracts import ProviderError
from backend.services.google_audio import GoogleTTSProvider


def profile(**values):
    return {'provider': 'google', 'project': 'fixture-project', 'location': 'eu',
            'language': 'cmn-CN', 'timeout_seconds': 1, **values}


class Auth:
    secrets = ('fixture-private-token',)

    def __init__(self):
        self.calls = 0

    async def request_headers(self):
        self.calls += 1
        return {'Authorization': 'Bearer fixture-private-token', 'x-goog-user-project': 'fixture-project'}


class Call:
    def __init__(self, chunks=None, block=False):
        self.texts = []
        self.queue = asyncio.Queue()
        self.chunks = chunks
        self.block = block
        self.configured = self.cancelled = self.finished = False

    async def configure(self):
        self.configured = True

    async def send_text(self, text):
        assert self.configured
        self.texts.append(text)
        if not self.block:
            for chunk in self.chunks if self.chunks is not None else [b'\1\0' * 24]:
                self.queue.put_nowait(chunk)

    async def finish(self):
        self.finished = True
        if not self.block:
            self.queue.put_nowait(None)

    async def receive(self):
        result = await self.queue.get()
        if isinstance(result, BaseException):
            raise result
        return result

    def cancel(self):
        self.cancelled = True
        self.queue.put_nowait(asyncio.CancelledError())


class Transport:
    def __init__(self, chunks=None, block=False):
        self.calls, self.options = [], []
        self.ready_count = 0
        self.closed = False
        self.chunks, self.block = chunks, block

    async def ready(self):
        self.ready_count += 1

    def start(self, **options):
        self.options.append(options)
        call = Call(self.chunks, self.block)
        self.calls.append(call)
        return call

    async def aclose(self):
        self.closed = True


def provider(*, transport=None, auth=None, **values):
    return GoogleTTSProvider(profile(**values), transport=transport or Transport(), auth=auth or Auth())


async def texts(*chunks):
    for chunk in chunks:
        yield chunk


async def collect(tts, source=None, **kwargs):
    return [item async for item in tts.stream_synthesize(source if source is not None else texts('你好。'), **kwargs)]


async def test_first_clause_is_sent_and_playable_before_remainder_arrives():
    continue_text = asyncio.Event()
    source_closed = asyncio.Event()

    async def source():
        try:
            yield '好的，'
            await continue_text.wait()
            yield '我来帮你调整。'
        finally:
            source_closed.set()

    transport = Transport()
    tts = provider(transport=transport)
    assert isinstance(tts, TTSProvider)
    stream = tts.stream_synthesize(source())
    first = await asyncio.wait_for(anext(stream), .5)
    assert first == {'type': 'audio', 'audio': b'\1\0' * 24,
                     'sample_rate': 24000, 'channels': 1, 'format': 'pcm_s16le'}
    assert transport.calls[0].texts == ['好的，']
    assert not transport.calls[0].finished and not source_closed.is_set()
    continue_text.set()
    remaining = [item async for item in stream]
    assert transport.calls[0].texts == ['好的，', '我来帮你调整。']
    assert source_closed.is_set() and transport.calls[0].cancelled
    assert remaining[-1]['text_chunks'] == 2 and remaining[-1]['audio_bytes'] == 96
    assert remaining[-1]['first_text_to_audio_ms'] >= 0
    assert all(item['type'] != 'alignment' for item in [first, *remaining])
    assert tts.info()['capabilities']['alignment'] is False
    assert transport.options[0]['metadata'] == (('authorization', 'Bearer fixture-private-token'),
                                               ('x-goog-user-project', 'fixture-project'))


async def test_synthesize_with_metrics_uses_same_stream_and_wraps_actual_pcm_as_wav():
    transport = Transport()
    tts = provider(transport=transport)
    result = await tts.synthesize_with_metrics('你好。')
    assert len(transport.calls) == 1 and transport.calls[0].texts == ['你好。']
    assert result['content_type'] == 'audio/wav'
    assert result['first_packet_ms'] <= result['total_ms']
    with wave.open(io.BytesIO(result['audio'])) as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (24000, 1, 2)
        assert audio.readframes(audio.getnframes()) == b'\1\0' * 24


async def test_prewarm_refreshes_credentials_and_connects_without_synthesizing():
    transport, auth = Transport(), Auth()
    tts = provider(transport=transport, auth=auth)
    result = await tts.prewarm_stream()
    assert result['ok'] and result['synthesis_requests'] == 0
    assert auth.calls == transport.ready_count == 1 and not transport.calls
    assert tts.info()['authentication_verified'] is True


@pytest.mark.parametrize('voice', [None, '', 'assistant', 'default', '小何2.0', 'xiaohe2.0', 'zh_female_xiaohe_uranus_bigtts'])
async def test_selected_default_and_persisted_legacy_default_use_configured_google_voice(voice):
    transport = Transport()
    await collect(provider(transport=transport, voices={'default': 'Aoede'}), voice=voice)
    assert transport.options[0]['voice'] == 'cmn-CN-Chirp3-HD-Aoede'


async def test_named_alias_and_full_chirp_identifier_configuration():
    transport = Transport()
    await collect(provider(transport=transport, voices={'warm': 'cmn-CN-Chirp3-HD-Leda'}), voice='warm')
    assert transport.options[0]['voice'] == 'cmn-CN-Chirp3-HD-Leda'
    with pytest.raises(ProviderError) as caught:
        await collect(provider(), voice='unconfigured')
    assert caught.value.code == 'unsupported_capability'


@pytest.mark.parametrize('values', [
    {'location': 'arbitrary.example'}, {'base_url': 'https://collector.example'},
    {'base_url_env': 'TTS_ENDPOINT'}, {'timeout_seconds': 0}, {'timeout_seconds': float('nan')},
    {'voices': {'assistant': 'en-US-Chirp3-HD-Kore'}}, {'voices': {'assistant': ''}},
    {'language': 'cmn-CN/extra'},
])
def test_invalid_configuration_fails_without_network(values):
    with pytest.raises(ValueError):
        provider(**values)


async def test_missing_project_and_empty_input_do_not_start_synthesis():
    transport, auth = Transport(), Auth()
    tts = provider(transport=transport, auth=auth, project='')
    assert not tts.info()['configured']
    with pytest.raises(ProviderError) as caught:
        await tts.prewarm_stream()
    assert caught.value.code == 'not_configured' and not auth.calls
    tts = provider(transport=transport, auth=auth)
    assert await collect(tts, texts(' ', '')) == []
    assert not auth.calls and not transport.calls
    with pytest.raises(ProviderError):
        await tts.synthesize_with_metrics(' ')


async def test_cancellation_closes_call_sender_and_input_without_closing_shared_transport():
    source_closed = asyncio.Event()
    transport = Transport(block=True)
    tts = provider(transport=transport)

    async def source():
        try:
            yield '第一段。'
            await asyncio.Event().wait()
        finally:
            source_closed.set()

    job = asyncio.create_task(collect(tts, source()))
    while not transport.calls or not transport.calls[0].texts:
        await asyncio.sleep(0)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert transport.calls[0].cancelled and source_closed.is_set()
    assert not tts._calls and not tts._senders and not transport.closed


async def test_consumer_aclose_cancels_upstream_immediately():
    transport = Transport()
    source_closed = asyncio.Event()

    async def source():
        try:
            yield '第一段。'
            await asyncio.Event().wait()
        finally:
            source_closed.set()

    tts = provider(transport=transport)
    stream = tts.stream_synthesize(source())
    await anext(stream)
    await stream.aclose()
    assert source_closed.is_set() and transport.calls[0].cancelled
    assert not tts._senders


async def test_close_interrupts_pending_first_text_without_starting_rpc():
    entered, closed = asyncio.Event(), asyncio.Event()

    async def source():
        try:
            entered.set()
            await asyncio.Event().wait()
            yield '不可达'
        finally:
            closed.set()

    tts = provider()
    job = asyncio.create_task(collect(tts, source()))
    await entered.wait()
    await tts.aclose()
    with pytest.raises(ProviderError) as caught:
        await job
    assert caught.value.code == 'unavailable' and closed.is_set()
    await tts.aclose()


async def test_auth_timeout_is_bounded_and_never_starts_transport():
    class BlockedAuth(Auth):
        async def request_headers(self):
            await asyncio.Event().wait()
    transport = Transport()
    with pytest.raises(ProviderError) as caught:
        await collect(provider(transport=transport, auth=BlockedAuth(), timeout_seconds=.02))
    assert caught.value.code == 'timeout' and not transport.calls


async def test_stream_deadline_and_empty_packets_cannot_keep_request_alive():
    class EmptyCall(Call):
        async def receive(self):
            await asyncio.sleep(.001)
            return b''
    transport = Transport()
    call = EmptyCall()
    transport.start = lambda **kwargs: call
    with pytest.raises(ProviderError) as caught:
        await collect(provider(transport=transport, timeout_seconds=.02))
    assert caught.value.code == 'timeout' and call.cancelled


async def test_downstream_pause_does_not_cancel_consumer_task():
    tts = provider(timeout_seconds=.02)
    stream = tts.stream_synthesize(texts('第一段。'))
    await anext(stream)
    await asyncio.sleep(.04)  # No timeout context spans the yield into this code.
    with pytest.raises(ProviderError) as caught:
        await anext(stream)
    assert caught.value.code == 'timeout'


async def test_odd_pcm_chunk_boundary_is_reassembled_but_truncated_pcm_is_rejected():
    items = await collect(provider(transport=Transport(chunks=[b'\1', b'\0\2\0'])))
    assert b''.join(item['audio'] for item in items if item['type'] == 'audio') == b'\1\0\2\0'
    for chunks in ([b'\1'], []):
        with pytest.raises(ProviderError) as caught:
            await collect(provider(transport=Transport(chunks=chunks)))
        assert caught.value.code == 'invalid_response'


async def test_bad_later_input_remains_invalid_request_instead_of_cancellation():
    tts = provider(transport=Transport(block=True))
    with pytest.raises(ProviderError) as caught:
        await collect(tts, texts('第一段。', '字' * 2000))
    assert caught.value.code == 'invalid_request'


async def test_upstream_eof_cancels_blocked_text_input_instead_of_waiting_for_deadline():
    closed = asyncio.Event()

    async def source():
        try:
            yield '第一段。'
            await asyncio.Event().wait()
        finally:
            closed.set()

    class EarlyCall(Call):
        async def send_text(self, text):
            self.texts.append(text)
            self.queue.put_nowait(None)

    transport, call = Transport(), EarlyCall()
    transport.start = lambda **kwargs: call
    with pytest.raises(ProviderError) as caught:
        await asyncio.wait_for(collect(provider(transport=transport), source()), .2)
    assert caught.value.code == 'invalid_response' and closed.is_set() and call.cancelled


@pytest.mark.parametrize('grpc_code,expected', [('UNAUTHENTICATED', 'auth'), ('PERMISSION_DENIED', 'permission_denied'),
    ('RESOURCE_EXHAUSTED', 'rate_limit'), ('UNAVAILABLE', 'unavailable'), ('DEADLINE_EXCEEDED', 'timeout')])
async def test_grpc_error_is_typed_without_leaking_tokens_or_metadata(grpc_code, expected):
    class RpcFailure(Exception):
        def code(self):
            return SimpleNamespace(name=grpc_code)
    transport = Transport(chunks=[RpcFailure('fixture-private-token Authorization: unknown-secret')])
    with pytest.raises(ProviderError) as caught:
        await collect(provider(transport=transport))
    error = caught.value
    assert error.code == expected and error.details['grpc_code'] == grpc_code
    assert 'fixture-private-token' not in json.dumps(error.public()) + json.dumps(error.details)
    assert 'unknown-secret' not in str(error)


async def test_callback_observes_only_normalized_events_and_failure_closes_stream():
    events = []
    tts = provider()
    await collect(tts, on_event=events.append)
    assert [item['type'] for item in events] == ['audio', 'metrics']

    async def callback(_item):
        raise RuntimeError('private-callback-detail')
    with pytest.raises(ProviderError):
        await collect(tts, on_event=callback)
    assert not tts._calls and not tts._senders


@pytest.mark.parametrize('cancel', [False, True])
async def test_input_close_error_is_redacted_and_does_not_override_cancellation(cancel):
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
            raise RuntimeError('fixture-private-token unknown-sensitive-detail')

    job = asyncio.create_task(collect(provider(), Source()))
    await entered.wait()
    if cancel:
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
    else:
        with pytest.raises(ProviderError) as caught:
            await job
        assert 'fixture-private-token' not in str(caught.value)
        assert 'unknown-sensitive-detail' not in json.dumps(caught.value.details)


async def test_sdk_message_shape_uses_pcm24k_and_config_then_text():
    types = pytest.importorskip('google.cloud.texttospeech_v1')
    from backend.services.google_audio import _GoogleStream

    class RawCall:
        def __init__(self):
            self.messages = []

        async def write(self, message):
            self.messages.append(message)

        async def done_writing(self):
            self.finished = True

    raw = RawCall()
    stream = _GoogleStream(raw, types, 'cmn-CN-Chirp3-HD-Kore', 'cmn-CN')
    await stream.configure()
    await stream.send_text('第一段，')
    await stream.finish()
    first, second = raw.messages
    assert first.streaming_config.streaming_audio_config.audio_encoding == types.AudioEncoding.PCM
    assert first.streaming_config.streaming_audio_config.sample_rate_hertz == 24000
    assert first.streaming_config.voice.language_code == 'cmn-CN'
    assert not first.input.text and not second.streaming_config.voice.name
    assert second.input.text == '第一段，' and raw.finished
