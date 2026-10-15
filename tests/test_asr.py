"""Offline ASR wire tests. Fakes never provide application transcriptions."""
import asyncio
import gzip
import json
import struct
from types import SimpleNamespace

import pytest

from backend.asr import (
    API_KEY_FIELD, APP_ID_FIELD, ACCESS_TOKEN_FIELD, CONFIG_FILE_FIELD,
    RESOURCE_FIELD, TIMEOUT_FIELD, ASR_URL, ASR_RESOURCE_ID, MAX_AUDIO_BYTES,
    VolcanoASR, decode_response, encode_audio, encode_full_request,
)


@pytest.fixture(autouse=True)
def no_ambient_asr(monkeypatch):
    for name in (API_KEY_FIELD, APP_ID_FIELD, ACCESS_TOKEN_FIELD, CONFIG_FILE_FIELD,
                 RESOURCE_FIELD, TIMEOUT_FIELD):
        monkeypatch.delenv(name, raising=False)


def response(text=None, *, sequence=1, final=False, flags=None, data=None,
             compressed=True, error=None, raw_payload=None, serialization=1):
    if data is None:
        data = {} if text is None else {'result': {'text': text}}
    payload = raw_payload if raw_payload is not None else json.dumps(data, ensure_ascii=False).encode()
    if compressed:
        payload = gzip.compress(payload)
    if error is not None:
        return bytes([0x11, 0xF0, (serialization << 4) | int(compressed), 0]) + struct.pack('>II', error, len(payload)) + payload
    if flags is None:
        flags = 3 if final else 1
    seq = struct.pack('>i', sequence) if flags & 1 else b''
    return bytes([0x11, 0x90 | flags, (serialization << 4) | int(compressed), 0]) + seq + struct.pack('>I', len(payload)) + payload


def request(raw):
    sequence, size = struct.unpack_from('>iI', raw, 4)
    assert size == len(raw) - 12
    payload = gzip.decompress(raw[12:])
    return {'type': raw[1] >> 4, 'flags': raw[1] & 15, 'sequence': sequence,
            'serialization': raw[2] >> 4, 'compression': raw[2] & 15,
            'payload': json.loads(payload) if raw[2] >> 4 else payload}


class FakeWS:
    def __init__(self, *, on_send=None):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.close_count = 0
        self.recv_count = 0
        self.on_send = on_send
        self.response = SimpleNamespace(headers={'X-Tt-Logid': 'offline-log-id'})

    async def send(self, raw):
        self.sent.append(raw)
        if self.on_send:
            await self.on_send(self, request(raw))

    async def recv(self):
        self.recv_count += 1
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self):
        self.closed = True
        self.close_count += 1


def adapter(ws=None, **kwargs):
    ws = ws or FakeWS()
    calls = []

    async def connect(url, **options):
        calls.append((url, options))
        return ws

    kwargs.setdefault('api_key', 'offline-asr-secret')
    kwargs.setdefault('timeout_seconds', .2)
    return VolcanoASR(ws_connect=connect, **kwargs), ws, calls


def test_info_independent_credentials_and_safe_unverified_configuration(monkeypatch):
    monkeypatch.setenv('VOLCENGINE_SPEECH_API_KEY', 'tts-only-key')
    monkeypatch.setenv('ARK_API_KEY', 'ark-only-key')
    asr = VolcanoASR()
    assert asr.info() == {
        'configured': False, 'missing_fields': [API_KEY_FIELD],
        'provider': 'volcengine-speech-asr', 'sample_rate': 16000, 'channels': 1,
        'format': 'pcm_s16le', 'resource_id': ASR_RESOURCE_ID, 'source': 'environment',
    }
    with pytest.raises(RuntimeError, match=API_KEY_FIELD):
        asr._headers('request')
    assert 'tts-only-key' not in str(asr.info())
    configured = VolcanoASR(api_key='asr-secret', resource_id='volc.bigasr.sauc.duration')
    assert configured.info()['configured']
    assert configured.info()['resource_id'] == 'volc.bigasr.sauc.duration'
    assert 'asr-secret' not in str(configured.info())


@pytest.mark.parametrize('values,missing', [
    ({'app_id': 'old-app'}, ACCESS_TOKEN_FIELD),
    ({'access_token': 'old-token'}, APP_ID_FIELD),
    ({'api_key': 'key', 'resource_id': ''}, RESOURCE_FIELD),
])
def test_missing_auth_parts_and_explicit_empty_resource(values, missing):
    assert missing in VolcanoASR(**values).info()['missing_fields']


def test_config_file_is_literal_allowlist_and_environment_overrides(tmp_path, monkeypatch):
    file = tmp_path / '.env'
    file.write_text('VOLC_ASR_API_KEY=authorized-file-secret\n'
                    'VOLC_ASR_RESOURCE_ID=volc.bigasr.sauc.duration\n'
                    'VOLC_ASR_ENDPOINT=wss://legacy/nostream\n'
                    'VOLC_ASR_LANGUAGE=ar-SA\n'
                    'UNRELATED_KEY=do-not-use\n')
    monkeypatch.setenv(CONFIG_FILE_FIELD, str(file))
    asr = VolcanoASR()
    assert asr.info()['configured'] and asr.info()['source'] == 'config_file'
    assert asr.info()['resource_id'] == 'volc.bigasr.sauc.duration'
    assert asr._headers('r')['X-Api-Key'] == 'authorized-file-secret'
    assert 'authorized-file-secret' not in asr.safe_message(RuntimeError('authorized-file-secret'))
    assert 'legacy' not in str(asr.info()) and 'ar-SA' not in str(asr.info())
    monkeypatch.setenv(API_KEY_FIELD, 'environment-secret')
    monkeypatch.setenv(RESOURCE_FIELD, ASR_RESOURCE_ID)
    monkeypatch.setenv(APP_ID_FIELD, '${UNUSED_LEGACY_APP}')
    monkeypatch.setenv(ACCESS_TOKEN_FIELD, 'placeholder')
    override = VolcanoASR()
    assert override.info()['configured'] and override.info()['source'] == 'environment'
    assert override._headers('r')['X-Api-Key'] == 'environment-secret'
    assert override.info()['resource_id'] == ASR_RESOURCE_ID
    # Even an overridden file credential must not leak if a provider echoes it.
    assert 'authorized-file-secret' not in override.safe_message('authorized-file-secret')
    monkeypatch.delenv(API_KEY_FIELD)
    monkeypatch.setenv(APP_ID_FIELD, 'environment-app')
    monkeypatch.setenv(ACCESS_TOKEN_FIELD, 'environment-token')
    legacy = VolcanoASR()
    assert legacy.info()['source'] == 'environment'
    assert legacy._headers('r')['X-Api-App-Key'] == 'environment-app'
    assert 'X-Api-Key' not in legacy._headers('r')
    assert file.read_text().startswith('VOLC_ASR_API_KEY=authorized-file-secret\n')


@pytest.mark.parametrize('content,field', [
    ('UNRELATED_KEY=value\n', API_KEY_FIELD),
    ('VOLC_ASR_API_KEY=your_api_key\n', API_KEY_FIELD),
    ('VOLC_ASR_API_KEY=${EXISTING_SECRET}\n', API_KEY_FIELD),
    ('VOLC_ASR_APP_ID=app\n', ACCESS_TOKEN_FIELD),
])
def test_bad_or_incomplete_file_never_executes_or_interpolates(tmp_path, monkeypatch, content, field):
    file = tmp_path / '.env'
    file.write_text(content)
    monkeypatch.setenv(CONFIG_FILE_FIELD, str(file))
    monkeypatch.setenv('EXISTING_SECRET', 'never-interpolate')
    asr = VolcanoASR()
    assert not asr.info()['configured']
    assert field in str(asr.info())
    assert 'never-interpolate' not in str(asr.info())
    with pytest.raises(RuntimeError):
        asr._headers('request')


def test_missing_and_oversized_files_are_safe_and_explicit_auth_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_FILE_FIELD, str(tmp_path / 'missing'))
    assert '不可读取' in VolcanoASR().info()['error']
    explicit = VolcanoASR(api_key='explicit-key')
    assert explicit.info()['configured']
    file = tmp_path / '.env'
    file.write_bytes(b'x' * (1024 * 1024 + 1))
    monkeypatch.setenv(CONFIG_FILE_FIELD, str(file))
    assert '1 MiB' in VolcanoASR().info()['error']


def test_safe_message_masks_all_known_credentials_and_header_values(monkeypatch):
    for name, value in {'ARK_API_KEY': 'ark-secret', 'TAVILY_API_KEY': 'tavily-secret',
                        'VOLCENGINE_SPEECH_API_KEY': 'tts-secret'}.items():
        monkeypatch.setenv(name, value)
    asr = VolcanoASR(api_key='asr-secret', app_id='asr-app', access_token='asr-token')
    unsafe = 'asr-secret asr-app asr-token ark-secret tavily-secret tts-secret Bearer unknown-secret, X-Api-Key: unknown-key'
    safe = asr.safe_message(unsafe)
    assert '[redacted]' in safe
    assert all(secret not in safe for secret in ('asr-secret', 'asr-app', 'asr-token', 'ark-secret',
                                                'tavily-secret', 'tts-secret', 'unknown-secret', 'unknown-key'))


@pytest.mark.parametrize('timeout', ['oops', 'nan', 0, -1])
def test_invalid_timeout_is_safe_configuration_error(timeout):
    asr = VolcanoASR(api_key='key', timeout_seconds=timeout)
    assert not asr.info()['configured'] and TIMEOUT_FIELD in asr.info()['error']


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
async def test_open_is_config_sent_boundary_without_waiting_for_provider(legacy):
    kwargs = {'api_key': '', 'app_id': 'old-app', 'access_token': 'old-token'} if legacy else {}
    asr, ws, calls = adapter(**kwargs)
    connection = await asr.open('request-id')
    try:
        assert ws.recv_count == 0 and len(ws.sent) == 1
        url, options = calls[0]
        assert url == ASR_URL and options['compression'] is None
        headers = options['additional_headers']
        assert headers['X-Api-Resource-Id'] == ASR_RESOURCE_ID
        assert headers['X-Api-Request-Id'] == 'request-id' and headers['X-Api-Sequence'] == '-1'
        assert 'X-Api-App-Id' not in headers
        if legacy:
            assert headers['X-Api-App-Key'] == 'old-app' and headers['X-Api-Access-Key'] == 'old-token'
            assert 'X-Api-Key' not in headers
        else:
            assert headers['X-Api-Key'] == 'offline-asr-secret'
        full = request(ws.sent[0])
        assert full['type'] == 1 and full['sequence'] == 1 and full['flags'] == 1
        assert full['payload'] == {'user': {'uid': 'request-id'},
                                   'audio': {'format': 'pcm', 'codec': 'raw', 'rate': 16000, 'bits': 16, 'channel': 1},
                                   'request': {'model_name': 'bigmodel', 'result_type': 'full'}}
        assert 0 <= connection.metrics['connect_ms'] <= connection.metrics['config_sent_ms']
    finally:
        await asr.aclose()
    assert ws.closed


@pytest.mark.asyncio
async def test_audio_uses_positive_sequences_then_empty_negative_final_packet_once():
    asr, ws, _ = adapter()
    connection = await asr.open('request')
    await connection.send_audio(b'\x01\x00' * 3200)
    await connection.send_audio(b'\x02\x00' * 400)
    await connection.finish()
    await connection.finish()
    frames = [request(raw) for raw in ws.sent]
    assert [frame['sequence'] for frame in frames] == [1, 2, 3, -4]
    assert frames[-1] == {'type': 2, 'flags': 3, 'sequence': -4, 'serialization': 0, 'compression': 1, 'payload': b''}
    assert connection.metrics['audio_bytes'] == 7200 and connection.metrics['audio_ms'] == 225
    with pytest.raises(RuntimeError, match='结束'):
        await connection.send_audio(b'\x00\x00')
    await asr.aclose()


@pytest.mark.asyncio
async def test_full_text_snapshots_and_utterance_definite_are_only_partial_until_terminal():
    asr, ws, _ = adapter()
    connection = await asr.open('request')
    stream = connection.results()
    ws.incoming.put_nowait(response(data={'result': {'text': '上海', 'utterances': [{'definite': True, 'text': '上海'}]}}))
    first = await anext(stream)
    assert first['type'] == 'partial' and first['text'] == '上海'
    assert not ws.closed
    # Repeated snapshot is ignored; final is authoritative even if unchanged.
    ws.incoming.put_nowait(response('上海', sequence=2))
    ws.incoming.put_nowait(response(data={'result': [{'text': '上海虹桥站'}]}, sequence=3))
    assert (await anext(stream))['text'] == '上海虹桥站'
    await connection.finish()
    ws.incoming.put_nowait(response('上海虹桥站', sequence=-4, final=True))
    final = await anext(stream)
    assert final['type'] == 'final' and final['text'] == '上海虹桥站'
    assert final['metrics']['server_sequence'] == -4
    assert final['metrics']['final_ms'] >= final['metrics']['first_partial_ms']
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert ws.closed and not asr._connections


@pytest.mark.asyncio
@pytest.mark.parametrize('flags,sequence', [(2, 0), (3, 3), (3, -3)])
async def test_request_terminal_flag_handles_no_sequence_and_official_positive_example(flags, sequence):
    asr, ws, _ = adapter()
    connection = await asr.open('request')
    await connection.finish()
    ws.incoming.put_nowait(response('最终文本', flags=flags, sequence=sequence))
    items = [item async for item in connection.results()]
    assert [(item['type'], item['text']) for item in items] == [('final', '最终文本')]
    assert ws.closed


@pytest.mark.asyncio
async def test_terminal_missing_text_uses_observed_snapshot_but_empty_text_is_not_invented():
    asr, ws, _ = adapter()
    connection = await asr.open('request')
    stream = connection.results()
    ws.incoming.put_nowait(response('先前文本'))
    assert (await anext(stream))['text'] == '先前文本'
    await connection.finish()
    ws.incoming.put_nowait(response(data={}, final=True, sequence=-2))
    assert (await anext(stream))['text'] == '先前文本'
    await stream.aclose()
    asr2, ws2, _ = adapter()
    conn2 = await asr2.open('empty')
    await conn2.finish()
    ws2.incoming.put_nowait(response('', final=True, sequence=-1))
    assert [item['text'] async for item in conn2.results()] == ['']


@pytest.mark.asyncio
async def test_final_before_user_stop_is_rejected_and_connection_closed():
    asr, ws, _ = adapter()
    connection = await asr.open('request')
    ws.incoming.put_nowait(response('提前', final=True, sequence=-2))
    with pytest.raises(RuntimeError, match='提前终结'):
        await anext(connection.results())
    assert ws.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('errorframe', [True, False])
async def test_provider_errors_are_real_and_redacted(errorframe):
    asr, ws, _ = adapter()
    connection = await asr.open('request')
    data = {'message': 'unauthorized offline-asr-secret', 'code': 45000001}
    ws.incoming.put_nowait(response(data=data, error=45000001 if errorframe else None))
    with pytest.raises(RuntimeError, match='45000001') as exc:
        await anext(connection.results())
    assert 'offline-asr-secret' not in str(exc.value) and ws.closed


@pytest.mark.asyncio
async def test_closed_transport_without_terminal_never_promotes_partial_to_final():
    asr, ws, _ = adapter()
    connection = await asr.open('request')
    stream = connection.results()
    ws.incoming.put_nowait(response('未完成'))
    assert (await anext(stream))['type'] == 'partial'
    ws.incoming.put_nowait(ConnectionError('offline-asr-secret'))
    with pytest.raises(RuntimeError, match='未完成转写') as exc:
        await anext(stream)
    assert 'offline-asr-secret' not in str(exc.value) and ws.closed


@pytest.mark.asyncio
async def test_recording_can_be_silent_past_final_timeout_then_stop_timeout_is_bounded():
    asr, ws, _ = adapter(timeout_seconds=.02, max_record_seconds=.3)
    connection = await asr.open('request')
    pending = asyncio.create_task(anext(connection.results()))
    await asyncio.sleep(.035)
    assert not pending.done()  # optimized mode sends nothing when text is unchanged.
    await connection.send_audio(b'\x00\x00')
    await connection.finish()
    with pytest.raises(RuntimeError, match='停止后等待最终转写超时'):
        await pending
    assert ws.closed and not connection._pending


@pytest.mark.asyncio
async def test_capture_wall_time_is_bounded_even_without_audio_or_stop():
    asr, ws, _ = adapter(timeout_seconds=.2, max_record_seconds=.02)
    connection = await asr.open('request')
    with pytest.raises(RuntimeError, match='录音超时'):
        await anext(connection.results())
    assert ws.closed


@pytest.mark.asyncio
async def test_capture_byte_and_frame_limits_are_enforced_before_sending():
    asr, ws, _ = adapter(max_audio_bytes=8)
    connection = await asr.open('request')
    for invalid in (b'', b'x', 'pcm', b'\x00\x00' * 3201):
        with pytest.raises(ValueError):
            await connection.send_audio(invalid)
    await connection.send_audio(b'\x00\x00' * 4)
    with pytest.raises(ValueError, match='大小上限'):
        await connection.send_audio(b'\x00\x00')
    assert len(ws.sent) == 2
    assert VolcanoASR(api_key='key', max_audio_bytes=MAX_AUDIO_BYTES * 2).max_audio_bytes == MAX_AUDIO_BYTES
    await asr.aclose()


@pytest.mark.asyncio
async def test_cancel_pending_receive_closes_provider_and_cleans_tasks():
    asr, ws, _ = adapter()
    connection = await asr.open('request')
    task = asyncio.create_task(anext(connection.results()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ws.closed and not connection._pending and not asr._connections
    await connection.aclose()
    assert ws.close_count == 1


@pytest.mark.asyncio
async def test_adapter_close_cancels_all_existing_connections():
    asr, ws, _ = adapter()
    connection = await asr.open('request')
    task = asyncio.create_task(anext(connection.results()))
    await asyncio.sleep(0)
    await asr.aclose()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ws.closed and not asr._connections
    with pytest.raises(RuntimeError, match='已关闭'):
        await asr.open('new')


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [True, False])
async def test_init_cancel_or_failure_after_handshake_always_closes_socket(cancel):
    sending = asyncio.Event()

    async def on_send(ws, frame):
        sending.set()
        if cancel:
            await asyncio.Event().wait()
        raise OSError('offline-asr-secret')

    ws = FakeWS(on_send=on_send)
    asr, _, _ = adapter(ws)
    task = asyncio.create_task(asr.open('request'))
    await sending.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError) as exc:
            await task
        assert 'offline-asr-secret' not in str(exc.value)
    assert ws.closed and not asr._connections


@pytest.mark.asyncio
async def test_connect_timeout_and_send_timeout_do_not_leave_open_connections():
    async def connect(url, **options):
        await asyncio.Event().wait()
    asr = VolcanoASR(api_key='key', ws_connect=connect, timeout_seconds=.01)
    with pytest.raises(RuntimeError, match='TimeoutError'):
        await asr.open('request')
    assert not asr._connections

    async def send_timeout(ws, frame):
        if frame['type'] == 2:
            await asyncio.Event().wait()
    asr2, ws2, _ = adapter(FakeWS(on_send=send_timeout), timeout_seconds=.01)
    connection = await asr2.open('request')
    with pytest.raises(RuntimeError, match='TimeoutError'):
        await connection.send_audio(b'\x00\x00')
    assert ws2.closed


def test_binary_response_decoder_enforces_bounded_decompression_and_size():
    compressed = response(data={'result': {'text': 'x' * 5000}})
    with pytest.raises(RuntimeError, match='解压后响应超过'):
        decode_response(compressed, max_payload_bytes=200)
    with pytest.raises(RuntimeError, match='大小上限'):
        decode_response(response('x' * 200, compressed=False), max_payload_bytes=100)
    assert decode_response(response('中文', flags=0, compressed=False))['data']['result']['text'] == '中文'
    error = decode_response(response(raw_payload=b'bad request', error=45000001, serialization=0, compressed=False))
    assert error['error_code'] == 45000001 and error['data']['message'] == 'bad request'


@pytest.mark.parametrize('bad', [
    b'', b'not binary', 'text websocket message',
    bytes([0x21, 0x91, 0x11, 0]) + b'\x00' * 8,
    bytes([0x10, 0x91, 0x11, 0]) + b'\x00' * 8,
    bytes([0x11, 0xA1, 0x11, 0]) + b'\x00' * 8,
    bytes([0x11, 0x94, 0x11, 0]) + b'\x00' * 8,
    bytes([0x11, 0x91, 0x21, 0]) + b'\x00' * 8,
    bytes([0x11, 0x91, 0x12, 0]) + b'\x00' * 8,
    response('bad')[:-1], response('bad') + b'extra',
    response(sequence=-1), response(sequence=0),
    response(raw_payload=b'{not json', compressed=False),
    response(raw_payload=b'[]', compressed=False),
    response(raw_payload=b'\xff', compressed=False),
    response(raw_payload=b'not gzip')[:12] + b'not gzip',
])
def test_malformed_packets_raise_safe_protocol_errors(bad):
    with pytest.raises(RuntimeError):
        decode_response(bad)


def test_gzip_truncation_and_trailing_members_are_rejected():
    good = response('ok')
    for payload in (good[12:-1], good[12:] + gzip.compress(b'{}')):
        bad = good[:8] + struct.pack('>I', len(payload)) + payload
        with pytest.raises(RuntimeError, match='gzip'):
            decode_response(bad)


def test_encode_request_and_audio_are_binary_and_negative_stop_sequence():
    assert request(encode_full_request('request'))['payload']['request']['result_type'] == 'full'
    assert request(encode_audio(b'', 2, final=True))['sequence'] == -2
    with pytest.raises(ValueError):
        encode_audio(b'', 0)
