"""Offline V3 binary-protocol fakes; no fake audio powers the live application."""
import asyncio
import gzip
import json
import struct

import pytest

from backend.speech_stream import STREAM_TTS_URL, decode_frame, encode_request
from backend.tts import VolcanoTTS, XIAOHE_VOICE, TTS_RESOURCE_ID


def server_frame(event, data=None, *, session='session', audio=None, error=None, compressed=False):
    body = audio if audio is not None else json.dumps(data or {}, ensure_ascii=False).encode()
    if compressed:
        body = gzip.compress(body)
    header = bytes([0x11, 0xF0 if error is not None else (0xB4 if audio is not None else 0x94),
                    (0 if audio is not None else 0x10) | int(compressed), 0])
    prefix = struct.pack('>I', error if error is not None else event)
    if error is None:
        identifier = ('connection' if event < 100 else session).encode()
        prefix += struct.pack('>I', len(identifier)) + identifier
    return header + prefix + struct.pack('>I', len(body)) + body


class FakeWS:
    def __init__(self, *, on_text=None, finish_audio=None):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.on_text = on_text
        self.finish_audio = finish_audio
        self.session = None

    async def send(self, raw):
        frame = decode_frame(raw)
        self.sent.append(frame)
        event = frame['event']
        if event == 1:
            self.incoming.put_nowait(server_frame(50))
        elif event == 100:
            self.session = frame['identifier']
            self.incoming.put_nowait(server_frame(150, session=self.session))
        elif event == 200:
            if self.on_text:
                await self.on_text(self, frame['data']['req_params']['text'])
            else:
                self.incoming.put_nowait(server_frame(352, session=self.session, audio=b'\x01\x00' * 12))
        elif event == 102:
            if self.finish_audio is not None:
                self.incoming.put_nowait(server_frame(352, session=self.session, audio=self.finish_audio))
            self.incoming.put_nowait(server_frame(152, {'usage': {'text_words': 10}}, session=self.session))

    async def recv(self):
        return await self.incoming.get()

    async def close(self):
        self.closed = True


def adapter(ws, *, timeout=1):
    calls = []

    async def connect(url, **kwargs):
        calls.append((url, kwargs))
        return ws

    return VolcanoTTS(api_key='offline-speech-secret', ws_connect=connect, timeout_seconds=timeout), calls


async def strings(*parts):
    for part in parts:
        yield part


def test_binary_request_uses_official_event_header_and_utf8_session_payload():
    assert encode_request(1) == bytes([17, 20, 16, 0, 0, 0, 0, 1, 0, 0, 0, 2, 123, 125])
    packet = encode_request(200, {'req_params': {'text': '上海虹桥站'}}, 'id')
    parsed = decode_frame(packet)
    assert parsed['event'] == 200 and parsed['identifier'] == 'id'
    assert parsed['data']['req_params']['text'] == '上海虹桥站'


def test_server_pcm_json_gzip_and_error_frame_decoding():
    assert decode_frame(server_frame(352, audio=b'\x01\x00'))['payload'] == b'\x01\x00'
    assert decode_frame(server_frame(350, {'text': '你好'}, compressed=True))['data'] == {'text': '你好'}
    error = decode_frame(server_frame(None, {'message': 'bad key'}, error=45000000))
    assert error['error_code'] == 45000000 and error['data']['message'] == 'bad key'
    with pytest.raises(RuntimeError, match='不完整'):
        decode_frame(server_frame(350)[:-2])


@pytest.mark.asyncio
async def test_real_incremental_contract_yields_pcm_before_input_finishes_and_no_audio_in_events():
    ws = FakeWS()
    tts, calls = adapter(ws)
    release = asyncio.Event()
    input_done = False
    observed = []

    async def text():
        nonlocal input_done
        yield '第一段。'
        await release.wait()
        yield '第二段。'
        input_done = True

    events, chunks = [], []
    try:
        async for item in tts.stream_synthesize(text(), on_event=observed.append):
            if item['type'] == 'audio':
                if not chunks:
                    assert not input_done
                    release.set()
                chunks.append(item)
            else:
                events.append(item)
        assert input_done and len(chunks) == 2
        assert all(item['format'] == 'pcm_s16le' and item['sample_rate'] == 24000 and item['channels'] == 1 for item in chunks)
        assert chunks[0]['nonzero'] and chunks[0]['first_nonzero_packet_ms'] is not None
        assert 0 <= chunks[0]['first_text_to_audio_ms'] <= chunks[0]['first_packet_ms']
        assert events == observed and all('audio' not in item for item in observed)
        assert events[-1]['event'] == 'session.finished'
        assert events[-1]['metrics']['audio_bytes'] == sum(len(item['audio']) for item in chunks)
        assert not ws.closed and len(tts._speech_pool) == 1
        url, kwargs = calls[0]
        assert url == STREAM_TTS_URL
        assert kwargs['additional_headers']['X-Api-Key'] == 'offline-speech-secret'
        assert kwargs['additional_headers']['X-Api-Resource-Id'] == TTS_RESOURCE_ID
        assert 'Authorization' not in kwargs['additional_headers']
        session = next(item for item in ws.sent if item['event'] == 100)
        params = session['data']['req_params']
        assert params['speaker'] == XIAOHE_VOICE
        assert params['audio_params'] == {'format': 'pcm', 'sample_rate': 24000}
        assert [item['data']['req_params']['text'] for item in ws.sent if item['event'] == 200] == ['第一段。', '第二段。']
    finally:
        await tts.aclose()
    assert ws.closed


@pytest.mark.asyncio
async def test_handshake_overlaps_wait_for_first_text():
    ws = FakeWS()
    tts, _ = adapter(ws)
    gate = asyncio.Event()

    async def text():
        await gate.wait()
        yield '稍后到达的文字。'

    stream = tts.stream_synthesize(text())
    try:
        assert (await anext(stream))['event'] == 'connection.started'
        assert not gate.is_set()
        assert not any(frame['event'] == 100 for frame in ws.sent)
        gate.set()
        assert (await anext(stream))['event'] == 'session.started'
        assert (await anext(stream))['type'] == 'audio'
    finally:
        await stream.aclose()
        await tts.aclose()


@pytest.mark.asyncio
async def test_stream_close_cancels_upstream_sender_and_closes_text_source():
    ws = FakeWS()
    tts, _ = adapter(ws)
    source_closed = asyncio.Event()

    async def text():
        try:
            yield '先说这一句。'
            await asyncio.Event().wait()
        finally:
            source_closed.set()

    stream = tts.stream_synthesize(text())
    try:
        while (await anext(stream))['type'] != 'audio':
            pass
        await stream.aclose()
        assert ws.closed and source_closed.is_set()
        assert 101 in [item['event'] for item in ws.sent]
        assert 2 in [item['event'] for item in ws.sent]
        assert not tts._speech_streams
    finally:
        await tts.aclose()


@pytest.mark.asyncio
async def test_provider_error_propagates_without_exposing_key():
    async def fail(ws, text):
        ws.incoming.put_nowait(server_frame(None, {'message': 'offline-speech-secret rejected'}, error=45000000))

    ws = FakeWS(on_text=fail)
    tts, _ = adapter(ws)
    try:
        with pytest.raises(RuntimeError, match='45000000') as captured:
            async for _ in tts.stream_synthesize(strings('你好')):
                pass
        assert 'offline-speech-secret' not in str(captured.value)
        assert ws.closed
    finally:
        await tts.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('parts', [('',), (' \n',), ('a' * 4001,), (None,)])
async def test_invalid_incremental_text_closes_socket_without_fake_audio(parts):
    ws = FakeWS(on_text=lambda *_: asyncio.sleep(0))
    tts, _ = adapter(ws)
    try:
        with pytest.raises(RuntimeError):
            async for _ in tts.stream_synthesize(strings(*parts)):
                pass
        assert ws.closed
    finally:
        await tts.aclose()


@pytest.mark.asyncio
async def test_pcm_sample_alignment_holds_only_one_byte_not_complete_audio():
    async def odd_chunks(ws, text):
        ws.incoming.put_nowait(server_frame(352, session=ws.session, audio=b'\x01'))
        ws.incoming.put_nowait(server_frame(352, session=ws.session, audio=b'\x02\x03\x04'))

    ws = FakeWS(on_text=odd_chunks)
    tts, _ = adapter(ws)
    try:
        audio = [item['audio'] async for item in tts.stream_synthesize(strings('你好')) if item['type'] == 'audio']
        assert audio == [b'\x01\x02\x03\x04']
    finally:
        await tts.aclose()


@pytest.mark.asyncio
async def test_zero_pcm_packet_is_distinguished_from_first_nonzero_packet():
    async def chunks(ws, text):
        ws.incoming.put_nowait(server_frame(352, session=ws.session, audio=b'\x00\x00' * 12))
        ws.incoming.put_nowait(server_frame(352, session=ws.session, audio=b'\x01\x00' * 12))

    ws = FakeWS(on_text=chunks)
    tts, _ = adapter(ws)
    try:
        audio = [item async for item in tts.stream_synthesize(strings('你好')) if item['type'] == 'audio']
        assert not audio[0]['nonzero'] and audio[0]['first_nonzero_packet_ms'] is None
        assert audio[1]['nonzero'] and audio[1]['first_nonzero_packet_ms'] >= audio[0]['first_packet_ms']
    finally:
        await tts.aclose()


@pytest.mark.asyncio
async def test_no_pcm_or_truncated_final_sample_is_an_explicit_error():
    for payload in (b'', b'\x01'):
        ws = FakeWS(on_text=lambda *_: asyncio.sleep(0), finish_audio=payload)
        tts, _ = adapter(ws)
        try:
            with pytest.raises(RuntimeError, match='有效音频|不完整采样点'):
                async for _ in tts.stream_synthesize(strings('你好')):
                    pass
            assert ws.closed
        finally:
            await tts.aclose()


@pytest.mark.asyncio
async def test_other_voice_is_rejected_before_websocket_connection():
    tts, calls = adapter(FakeWS())
    try:
        with pytest.raises(RuntimeError, match='仅支持'):
            async for _ in tts.stream_synthesize(strings('你好'), voice='zh_female_xiaohe_jupiter_bigtts'):
                pass
        assert not calls
    finally:
        await tts.aclose()


@pytest.mark.asyncio
async def test_prewarm_and_clean_session_reuse_skip_connection_handshake():
    ws = FakeWS()
    tts, calls = adapter(ws)
    try:
        warm = await tts.prewarm_stream()
        assert warm['ok'] and warm['pool_ready'] == 1
        assert [item['event'] for item in ws.sent] == [1]
        assert (await tts.prewarm_stream())['reused']
        for _ in range(2):
            result = [item async for item in tts.stream_synthesize(strings('真实输入形状。'))]
            assert result[0]['metrics']['reused']
            assert result[0]['metrics']['connect_ms'] == 0
            assert result[-1]['event'] == 'session.finished'
            assert len(tts._speech_pool) == 1 and not ws.closed
        assert len(calls) == 1
        assert [item['event'] for item in ws.sent].count(1) == 1
        assert [item['event'] for item in ws.sent].count(100) == 2
        assert len({item['identifier'] for item in ws.sent if item['event'] == 100}) == 2
    finally:
        await tts.aclose()
    assert ws.closed and ws.sent[-1]['event'] == 2


@pytest.mark.asyncio
async def test_prewarm_is_coalesced_and_does_not_create_concurrent_sessions():
    sockets = []

    async def connect(url, **kwargs):
        await asyncio.sleep(0)
        socket = FakeWS()
        sockets.append(socket)
        return socket

    tts = VolcanoTTS(api_key='offline', ws_connect=connect)
    try:
        await asyncio.gather(tts.prewarm_stream(), tts.prewarm_stream())
        assert len(sockets) == 1
        await asyncio.gather(
            asyncio.create_task(_consume(tts.stream_synthesize(strings('第一请求。')))),
            asyncio.create_task(_consume(tts.stream_synthesize(strings('第二请求。')))))
        assert len(sockets) == 2
        assert all([item['event'] for item in socket.sent].count(100) == 1 for socket in sockets)
        assert len(tts._speech_pool) == 2
    finally:
        await tts.aclose()


async def _consume(stream):
    return [item async for item in stream]


@pytest.mark.asyncio
async def test_cancelled_session_is_closed_instead_of_recycled():
    ws = FakeWS()
    tts, _ = adapter(ws)
    stop = asyncio.Event()

    async def source():
        yield '正在朗读。'
        await stop.wait()

    await tts.prewarm_stream()
    stream = tts.stream_synthesize(source())
    try:
        while (await anext(stream))['type'] != 'audio':
            pass
        await stream.aclose()
        assert ws.closed and not tts._speech_pool
        assert 101 in [item['event'] for item in ws.sent]
    finally:
        await tts.aclose()


@pytest.mark.asyncio
async def test_cancellation_before_first_packet_closes_socket_and_source():
    ws = FakeWS(on_text=lambda *_: asyncio.sleep(0))
    tts, _ = adapter(ws)
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def source():
        try:
            yield '等待真实音频。'
            entered.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    pending = asyncio.create_task(_consume(tts.stream_synthesize(source())))
    try:
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert ws.closed and closed.is_set() and not tts._speech_pool
    finally:
        await tts.aclose()


@pytest.mark.asyncio
async def test_pending_prewarm_wait_is_counted_separately_from_provider_session_clock():
    ws = FakeWS()
    entered, release = asyncio.Event(), asyncio.Event()

    async def connect(url, **kwargs):
        entered.set()
        await release.wait()
        return ws

    tts = VolcanoTTS(api_key='offline', ws_connect=connect)
    warm = asyncio.create_task(tts.prewarm_stream())
    try:
        await entered.wait()
        assert tts.info()['stream_prewarming'] and tts.info()['stream_pool_ready'] == 0
        pending = asyncio.create_task(_consume(tts.stream_synthesize(strings('计时样本。'))))
        await asyncio.sleep(0.01)
        release.set()
        await warm
        items = await pending
        metrics = items[-1]['metrics']
        assert metrics['prewarm_wait_ms'] >= 5
        assert metrics['lease_wait_ms'] >= metrics['prewarm_wait_ms']
        assert metrics['adapter_first_packet_ms'] >= metrics['first_packet_ms'] + metrics['prewarm_wait_ms']
        assert metrics['adapter_elapsed_ms'] >= metrics['provider_session_elapsed_ms'] + metrics['prewarm_wait_ms']
        assert tts.info()['stream_pool_ready'] == 1 and not tts.info()['stream_prewarming']
    finally:
        await tts.aclose()
