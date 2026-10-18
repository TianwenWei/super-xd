"""Offline contract fixtures exercise wire formats; no fixture is real recognition."""
import asyncio
from dataclasses import FrozenInstanceError
from email import policy
from email.parser import BytesParser
import io
import json
import wave

import httpx
import pytest

from super_xd.demo.audio.providers import AudioFormat, ASRProvider, ASRStream, TTSProvider, PCM16K_ASR, PCM24K_TTS, VLLMASRProvider, VolcengineTTSProvider, VolcengineASRProvider, build_asr, build_tts
from super_xd.nlp.contracts.providers import ProviderError


def profile(**values):
    return {'provider': 'vllm', 'base_url': 'http://127.0.0.1:9000/v1', 'model': 'FireRedASR2', **values}


def multipart(request):
    envelope = b'Content-Type: ' + request.headers['content-type'].encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + request.content
    message = BytesParser(policy=policy.default).parsebytes(envelope)
    return {part.get_param('name', header='content-disposition'): part for part in message.iter_parts()}


def test_audio_format_defines_pcm_lengths_and_rejects_invalid_samples():
    assert PCM16K_ASR.bytes_per_ms == 32 and PCM24K_TTS.bytes_per_ms == 48
    assert PCM16K_ASR.duration_ms(1024) == 32
    assert PCM24K_TTS.to_dict() == {'sample_rate': 24000, 'channels': 1, 'format': 'pcm_s16le'}
    with pytest.raises(FrozenInstanceError):
        PCM16K_ASR.sample_rate = 24000
    for invalid in (b'', b'\x01', None, 'audio'):
        with pytest.raises(ValueError):
            PCM16K_ASR.validate_chunk(invalid)
    for args in ((0, 1), (True, 1), (16000, 0), (16000, 1, 'mp3')):
        with pytest.raises(ValueError):
            AudioFormat(*args)


async def test_vllm_only_uploads_finalized_real_wav_with_model_language_and_auth(monkeypatch):
    monkeypatch.setenv('ASR_TEST_KEY', 'credential-for-local-test')
    requests = []
    pcm = b'\x01\x00\x02\x00' * 128
    def handler(request):
        requests.append(request)
        assert str(request.url) == 'http://127.0.0.1:9000/v1/audio/transcriptions'
        assert request.headers['authorization'] == 'Bearer credential-for-local-test'
        parts = multipart(request)
        assert parts['model'].get_payload(decode=True).decode() == 'FireRedASR2'
        assert parts['language'].get_payload(decode=True).decode() == 'zh'
        assert parts['response_format'].get_payload(decode=True) == b'json'
        assert parts['file'].get_filename() == 'utterance.wav'
        with wave.open(io.BytesIO(parts['file'].get_payload(decode=True))) as wav:
            assert (wav.getnchannels(), wav.getframerate(), wav.getsampwidth()) == (1, 16000, 2)
            assert wav.readframes(wav.getnframes()) == pcm
        return httpx.Response(200, json={'text': '测试返回的识别原文', 'api_key': 'credential-for-local-test'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = VLLMASRProvider(profile(key_env='ASR_TEST_KEY', require_api_key=True, language='zh'), client=http)
        assert isinstance(provider, ASRProvider)
        assert provider.info()['capabilities']['partial_results'] is False
        stream = await provider.open('segment1')
        assert isinstance(stream, ASRStream)
        await stream.send_audio(pcm)
        async def collect():
            return [item async for item in stream.results()]
        result = asyncio.create_task(collect())
        await asyncio.sleep(0)
        assert not requests and not result.done()
        await stream.finish()
        await stream.finish()  # Finalizing input is idempotent.
        events = await result
        assert len(events) == 1 and events[0]['type'] == 'final'
        assert events[0]['text'] == '测试返回的识别原文'
        assert events[0]['metrics']['first_partial_ms'] is None
        assert events[0]['metrics']['audio_ms'] == 16
        assert 'credential-for-local-test' not in json.dumps(events)
        assert stream._closed and not provider._connections
        await provider.aclose()
        assert not http.is_closed  # Injected transport remains owned by its caller.


async def test_vllm_can_explicitly_use_an_unauthenticated_local_service(monkeypatch):
    monkeypatch.delenv('OPTIONAL_ASR_KEY', raising=False)
    def handler(request):
        assert 'authorization' not in request.headers
        return httpx.Response(200, json={'text': ''})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = VLLMASRProvider(profile(key_env='OPTIONAL_ASR_KEY', require_api_key=False), client=http)
        assert provider.info()['configured'] is True
        stream = await provider.open('segment')
        await stream.send_audio(b'\0\0')
        await stream.finish()
        assert [item['text'] async for item in stream.results()] == ['']
        await provider.aclose()


async def test_missing_configuration_fails_before_network(monkeypatch):
    monkeypatch.delenv('MISSING_ASR_KEY', raising=False)
    def forbidden(request):
        raise AssertionError('Unconfigured service must not be contacted')
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        for values in ({'base_url': ''}, {'model': ''}, {'key_env': 'MISSING_ASR_KEY', 'require_api_key': True}):
            provider = VLLMASRProvider(profile(**values), client=http)
            assert provider.info()['configured'] is False
            with pytest.raises(ProviderError) as caught:
                await provider.open('segment')
            assert caught.value.code == 'not_configured'
            await provider.aclose()


@pytest.mark.parametrize('status,code', [(401, 'auth'), (403, 'auth'), (429, 'rate_limit'), (503, 'unavailable')])
async def test_vllm_http_failures_are_typed_and_never_become_transcripts(status, code):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as http:
        provider = VLLMASRProvider(profile(), client=http)
        stream = await provider.open('segment')
        await stream.send_audio(b'\0\0')
        await stream.finish()
        with pytest.raises(ProviderError) as caught:
            _ = [item async for item in stream.results()]
        assert caught.value.code == code and caught.value.provider == 'vllm'
        assert stream._closed and not provider._connections
        await provider.aclose()


@pytest.mark.parametrize('body', [b'no JSON', b'[]', b'{}', b'{"text": 123}', json.dumps({'text': '字' * 4001}).encode()])
async def test_vllm_invalid_payload_never_invents_text(body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))) as http:
        provider = VLLMASRProvider(profile(), client=http)
        stream = await provider.open('segment')
        await stream.send_audio(b'\0\0')
        await stream.finish()
        with pytest.raises(ProviderError) as caught:
            _ = [item async for item in stream.results()]
        assert caught.value.code == 'invalid_response'
        await provider.aclose()


async def test_vllm_cancel_discards_pending_upload_and_closes_only_its_stream():
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def handler(request):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = VLLMASRProvider(profile(), client=http)
        stream = await provider.open('old')
        successor = await provider.open('new')
        await stream.send_audio(b'\0\0')
        await stream.finish()
        async def collect():
            return [item async for item in stream.results()]
        job = asyncio.create_task(collect())
        await asyncio.wait_for(started.wait(), 1)
        await stream.aclose()
        await stream.aclose()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert cancelled.is_set() and not successor._closed
        assert provider._connections == {successor}
        await provider.aclose()
        assert successor._closed


async def test_vllm_rejects_wrong_frame_empty_audio_and_input_after_finish():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'text': 'fixture'}))) as http:
        provider = VLLMASRProvider(profile(max_record_seconds=.001), client=http)
        stream = await provider.open('segment')
        for pcm in (b'\x00', b'\0\0' * 3201):
            with pytest.raises(ProviderError):
                await stream.send_audio(pcm)
        await stream.finish()
        with pytest.raises(ProviderError):
            await stream.send_audio(b'\0\0')
        with pytest.raises(ProviderError):
            _ = [item async for item in stream.results()]
        await provider.aclose()


class TTSTransport:
    def __init__(self, events=None):
        self.events = events or []
        self.voices, self.closed = [], False
        self.stream_closed = asyncio.Event()
    def info(self):
        return {'configured': True, 'missing_fields': []}
    async def stream_synthesize(self, texts, voice=None):
        self.voices.append(voice)
        try:
            async for _ in texts:
                for item in self.events:
                    yield item
        finally:
            self.stream_closed.set()
    async def synthesize_with_metrics(self, text, voice=None):
        self.voices.append(voice)
        return {'audio': b'fixture-mp3', 'first_packet_ms': 1, 'total_ms': 2}
    async def prewarm_stream(self):
        return {'ok': True}
    async def aclose(self):
        self.closed = True


async def text_stream():
    yield '提供商实际返回的句子。'


async def test_tts_maps_logical_and_legacy_voices_and_normalizes_real_alignment():
    from super_xd.demo.audio.volcengine_tts import XIAOHE_VOICE
    transport = TTSTransport([
        {'type': 'event', 'event': 'sentence.started', 'data': {'text': '完整文本'}},
        {'type': 'audio', 'audio': b'\1\0' * 24},
        {'type': 'event', 'event': 'sentence.finished', 'data': {'text': '完整文本'}},
    ])
    provider = VolcengineTTSProvider(transport)
    assert isinstance(provider, TTSProvider)
    events = [item async for item in provider.stream_synthesize(text_stream(), voice='assistant')]
    alignment = next(item for item in events if item['type'] == 'alignment')
    assert alignment == {'type': 'alignment', 'text': '完整文本', 'start_ms': 0, 'end_ms': 1, 'precision': 'provider'}
    audio = next(item for item in events if item['type'] == 'audio')
    assert audio['sample_rate'] == 24000 and audio['channels'] == 1 and audio['format'] == 'pcm_s16le'
    assert transport.voices == [XIAOHE_VOICE]
    assert all('event' not in item for item in events)
    for voice in ('default', XIAOHE_VOICE, 'xiaohe2.0', '小何2.0'):
        await provider.synthesize_with_metrics('内容', voice)
    assert set(transport.voices) == {XIAOHE_VOICE}
    with pytest.raises(ProviderError):
        await provider.synthesize_with_metrics('内容', 'unconfigured_voice')
    await provider.aclose()


async def test_tts_does_not_fabricate_alignment_without_provider_events():
    provider = VolcengineTTSProvider(TTSTransport([{'type': 'audio', 'audio': b'\1\0'}]))
    events = [item async for item in provider.stream_synthesize(text_stream())]
    assert [item['type'] for item in events] == ['audio']
    await provider.aclose()


async def test_tts_cancel_closes_only_the_request_not_shared_provider():
    entered = asyncio.Event()
    async def pending_text():
        entered.set()
        await asyncio.Event().wait()
        yield 'unreachable'
    transport = TTSTransport()
    provider = VolcengineTTSProvider(transport)
    async def collect():
        return [item async for item in provider.stream_synthesize(pending_text())]
    job = asyncio.create_task(collect())
    await entered.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert transport.stream_closed.is_set() and not transport.closed
    await provider.aclose()


async def test_provider_factories_and_malformed_urls_fail_without_fallback():
    asr, tts = build_asr({'provider': 'volcengine'}), build_tts({'provider': 'volcengine'})
    assert isinstance(asr, VolcengineASRProvider) and isinstance(tts, VolcengineTTSProvider)
    await asr.aclose()
    await tts.aclose()
    for url in ('file:///private/key', 'http://user:secret@localhost/v1'):
        with pytest.raises(ValueError):
            VLLMASRProvider(profile(base_url=url))
    with pytest.raises(ValueError):
        build_asr({'provider': 'unknown'})
    with pytest.raises(ValueError):
        build_tts({'provider': 'unknown'})


@pytest.mark.parametrize('packet', [{'type': 'audio', 'audio': b'\1'},
                                  {'type': 'audio', 'audio': b'\1\0', 'sample_rate': 16000}])
async def test_tts_incompatible_pcm_is_a_provider_error_not_relabelled_audio(packet):
    provider = VolcengineTTSProvider(TTSTransport([packet]))
    with pytest.raises(ProviderError) as caught:
        _ = [item async for item in provider.stream_synthesize(text_stream())]
    assert caught.value.code == 'invalid_response'
    await provider.aclose()


async def test_tts_sentence_metadata_without_pcm_never_claims_heard_text():
    transport = TTSTransport([{'type': 'event', 'event': 'sentence.finished', 'data': {'text': '未播放文本'}}])
    provider = VolcengineTTSProvider(transport)
    events = [item async for item in provider.stream_synthesize(text_stream())]
    assert all(item['type'] != 'alignment' for item in events)
    await provider.aclose()


def test_audio_providers_are_backend_clients_not_device_queries():
    asr = VolcengineASRProvider(type('Adapter', (), {'info': lambda self: {'configured': False}})())
    tts = VolcengineTTSProvider(TTSTransport())
    assert asr.info()['execution_location'] == tts.info()['execution_location'] == 'cloud'
