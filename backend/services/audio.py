"""Audio service contracts and adapters; cockpit wire audio stays provider-neutral.

ASR input is 16 kHz mono PCM. TTS output is 24 kHz mono PCM. A buffered
transcription endpoint is explicitly advertised as utterance-only: accepting
chunks from the microphone does not mean the remote model streams results.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import io
import json
import math
import os
import time
from typing import Any, AsyncIterator, Protocol, TypedDict, runtime_checkable
from urllib.parse import urlparse
import wave

import httpx

from backend.services.contracts import ProviderError, redact
from backend.textutil import is_identifier


@dataclass(frozen=True)
class AudioFormat:
    sample_rate: int
    channels: int = 1
    format: str = 'pcm_s16le'

    def __post_init__(self):
        if type(self.sample_rate) is not int or self.sample_rate <= 0 or type(self.channels) is not int or self.channels <= 0:
            raise ValueError('Audio sample rate/channels must be positive integers')
        if self.format != 'pcm_s16le':
            raise ValueError('Only signed little-endian PCM16 is supported on the cockpit wire')

    @property
    def sample_width(self) -> int:
        return 2

    @property
    def bytes_per_ms(self) -> float:
        return self.sample_rate * self.channels * self.sample_width / 1000

    def duration_ms(self, byte_count: int) -> float:
        if type(byte_count) is not int or byte_count < 0:
            raise ValueError('Audio byte count must be a nonnegative integer')
        return byte_count / self.bytes_per_ms

    def to_dict(self) -> dict[str, Any]:
        return {'format': self.format, 'sample_rate': self.sample_rate, 'channels': self.channels}

    def validate_chunk(self, pcm: bytes, *, max_duration_ms: int | None = None) -> None:
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % (self.channels * self.sample_width):
            raise ValueError('Audio chunk must contain complete nonempty PCM16 sample frames')
        if max_duration_ms is not None and self.duration_ms(len(pcm)) > max_duration_ms:
            raise ValueError('Audio chunk exceeds the maximum frame duration')


PCM16K_ASR = AudioFormat(16000)
PCM24K_TTS = AudioFormat(24000)


class ASRResult(TypedDict):
    type: str
    text: str
    metrics: dict[str, Any]


class AudioChunk(TypedDict):
    type: str
    audio: bytes
    sample_rate: int
    channels: int
    format: str


class TextAlignment(TypedDict):
    type: str
    text: str
    start_ms: float
    end_ms: float
    precision: str


@runtime_checkable
class ASRStream(Protocol):
    metrics: dict[str, Any]
    async def send_audio(self, pcm: bytes) -> None: ...
    async def finish(self) -> None: ...
    def results(self) -> AsyncIterator[ASRResult]: ...
    async def aclose(self) -> None: ...


@runtime_checkable
class ASRProvider(Protocol):
    input_format: AudioFormat
    def info(self) -> dict[str, Any]: ...
    def safe_message(self, value: Any) -> str: ...
    async def open(self, request_id: str) -> ASRStream: ...
    async def aclose(self) -> None: ...


@runtime_checkable
class TTSProvider(Protocol):
    output_format: AudioFormat
    def info(self) -> dict[str, Any]: ...
    def stream_synthesize(self, texts: AsyncIterator[str], on_event=None, voice: str | None = None) -> AsyncIterator[dict[str, Any]]: ...
    async def synthesize_with_metrics(self, text: str, voice: str | None = None) -> dict[str, Any]: ...
    async def prewarm_stream(self) -> dict[str, Any]: ...
    async def aclose(self) -> None: ...


def _error(exc: Exception, provider: str, secrets=()) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    status = getattr(exc, 'status_code', None)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    known_code = getattr(exc, 'provider_code', None)
    if known_code in {'timeout', 'auth', 'rate_limit', 'unavailable', 'invalid_response', 'invalid_request'}:
        code = known_code
    elif isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        code = 'timeout'
    elif status in (401, 403):
        code = 'auth'
    elif status == 429:
        code = 'rate_limit'
    elif isinstance(status, int) and status >= 500:
        code = 'unavailable'
    elif isinstance(exc, (OSError, httpx.RequestError)):
        code = 'unavailable'
    elif isinstance(exc, (TypeError, ValueError)):
        code = 'invalid_request'
    else:
        code = 'provider_error'
    diagnostic = redact(str(exc), secrets=secrets)
    details = {'exception_type': type(exc).__name__, 'status_code': status, 'diagnostic': diagnostic}
    upstream = getattr(exc, 'upstream_diagnostics', None)
    if isinstance(upstream, dict):
        details['upstream'] = redact(upstream, secrets=secrets)
    return ProviderError(code, diagnostic or type(exc).__name__, provider=provider,
                         retryable=code in {'timeout', 'rate_limit', 'unavailable'},
                         details=details)


def _positive(profile, name, default):
    value = profile.get(name, default)
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(name + ' must be a positive finite number')
    return float(value)


def _secret(profile, field):
    name = profile.get(field)
    if name is None:
        return None
    if not isinstance(name, str) or not name or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_' for c in name):
        raise ValueError(field + ' must name an environment variable')
    return os.getenv(name, '')


class _VolcengineASRStream:
    def __init__(self, stream, provider):
        self.stream, self.provider = stream, provider
        self.metrics = stream.metrics

    async def send_audio(self, pcm):
        try:
            self.provider.input_format.validate_chunk(pcm, max_duration_ms=200)
            await self.stream.send_audio(pcm)
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except Exception as exc:
            raise _error(exc, self.provider.name, self.provider.secrets) from None

    async def finish(self):
        try:
            await self.stream.finish()
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except Exception as exc:
            raise _error(exc, self.provider.name, self.provider.secrets) from None

    async def results(self):
        try:
            async for item in self.stream.results():
                yield redact(item, secrets=self.provider.secrets)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _error(exc, self.provider.name, self.provider.secrets) from None
        finally:
            await self.aclose()

    async def aclose(self):
        await self.stream.aclose()


class VolcengineASRProvider:
    name = 'volcengine'
    input_format = PCM16K_ASR

    def __init__(self, adapter=None, *, profile=None):
        from backend.asr import VolcanoASR
        profile = profile or {}
        arguments = {key: value for key, value in {
            'api_key': _secret(profile, 'key_env'), 'app_id': _secret(profile, 'app_id_env'),
            'access_token': _secret(profile, 'access_token_env'),
            'resource_id': profile.get('resource_id'), 'timeout_seconds': profile.get('timeout_seconds'),
        }.items() if value is not None}
        self.adapter = adapter if adapter is not None else VolcanoASR(**arguments)
        self.secrets = tuple(value for value in (getattr(self.adapter, '_api_key', None), getattr(self.adapter, '_app_id', None),
                                                getattr(self.adapter, '_access_token', None)) if value)
        self._closing = False

    def info(self):
        return {**self.adapter.info(), 'provider': self.name, 'execution_location': 'cloud', 'input_format': self.input_format.to_dict(),
                'capabilities': {'partial_results': True, 'streaming_input': True, 'utterance_only': True}}

    def safe_message(self, value):
        return str(redact(self.adapter.safe_message(value), secrets=self.secrets))

    async def open(self, request_id):
        if self._closing:
            raise ProviderError('unavailable', 'ASR provider is closed', provider=self.name)
        if not self.info().get('configured'):
            raise ProviderError('not_configured', 'ASR credentials or resource configuration are missing', provider=self.name,
                                details={'status': self.info()})
        try:
            return _VolcengineASRStream(await self.adapter.open(request_id), self)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _error(exc, self.name, self.secrets) from None

    async def aclose(self):
        if self._closing:
            return
        self._closing = True
        await self.adapter.aclose()


class VLLMASRProvider:
    """Standard multipart /audio/transcriptions; never claims realtime support."""
    name = 'vllm'
    input_format = PCM16K_ASR

    def __init__(self, profile, *, client=None):
        self.base_url = profile.get('base_url') or ''
        if not isinstance(self.base_url, str):
            raise ValueError('vLLM ASR base_url must be a string')
        parsed = urlparse(self.base_url)
        if self.base_url and (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('vLLM ASR base_url must be an HTTP(S) service URL without credentials')
        self.model = profile.get('model') or ''
        if not isinstance(self.model, str):
            raise ValueError('vLLM ASR model must be a string')
        self.base_url_env, self.model_env = profile.get('base_url_env'), profile.get('model_env')
        self.require_api_key = profile.get('require_api_key', False)
        if type(self.require_api_key) is not bool:
            raise ValueError('require_api_key must be a boolean')
        self.language = profile.get('language')
        if self.language is not None and (not isinstance(self.language, str) or not self.language.strip()):
            raise ValueError('ASR language must be a nonblank string')
        self.timeout_seconds = _positive(profile, 'timeout_seconds', 60)
        self.max_record_seconds = _positive(profile, 'max_record_seconds', profile.get('max_audio_seconds', 60))
        if self.max_record_seconds > 60:
            raise ValueError('ASR max_record_seconds cannot exceed the 60-second cockpit limit')
        self.max_audio_bytes = int(self.input_format.bytes_per_ms * 1000 * self.max_record_seconds)
        self.key_env = profile.get('key_env')
        self.key = _secret(profile, 'key_env')
        self.secrets = (self.key,) if self.key else ()
        self.client = client if client is not None else httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds, connect=10))
        self._owns_client = client is None
        self._connections = set()
        self._closing = False

    def info(self):
        missing = []
        if not self.base_url.strip():
            missing.append(self.base_url_env or 'base_url')
        if not self.model.strip():
            missing.append(self.model_env or 'model')
        if self.require_api_key and not self.key:
            missing.append(self.key_env or 'key_env')
        return {'provider': self.name, 'execution_location': 'cloud', 'configured': not missing, 'missing_fields': missing, 'model': self.model,
                **self.input_format.to_dict(), 'input_format': self.input_format.to_dict(),
                'capabilities': {'partial_results': False, 'streaming_input': False, 'utterance_only': True}}

    def safe_message(self, value):
        return str(redact(str(value), secrets=self.secrets))

    async def open(self, request_id):
        if self._closing:
            raise ProviderError('unavailable', 'ASR provider is closed', provider=self.name)
        if not is_identifier(request_id, max_length=128, extra='_.:-', alnum_first=True):
            raise ProviderError('invalid_request', 'Invalid ASR request identifier', provider=self.name)
        if not self.info()['configured']:
            raise ProviderError('not_configured', 'ASR endpoint, model or required API key is not configured', provider=self.name,
                                details={'missing_fields': self.info()['missing_fields']})
        connection = VLLMASRStream(self, request_id)
        self._connections.add(connection)
        return connection

    async def _transcribe(self, request_id, pcm):
        audio = io.BytesIO()
        with wave.open(audio, 'wb') as wav:
            wav.setnchannels(self.input_format.channels)
            wav.setsampwidth(self.input_format.sample_width)
            wav.setframerate(self.input_format.sample_rate)
            wav.writeframes(pcm)
        data = {'model': self.model, 'response_format': 'json'}
        if self.language is not None:
            data['language'] = self.language
        headers = {'Authorization': 'Bearer ' + self.key} if self.key else {}
        try:
            async with self.client.stream('POST', self.base_url.rstrip('/') + '/audio/transcriptions',
                data=data, files={'file': ('utterance.wav', audio.getvalue(), 'audio/wav')},
                headers=headers, timeout=self.timeout_seconds) as response:
                response.raise_for_status()
                body = bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part)
                    if len(body) > 1024 * 1024:
                        raise ProviderError('invalid_response', 'ASR response exceeds the size limit', provider=self.name)
                try:
                    result = json.loads(body)
                except (ValueError, UnicodeError):
                    raise ProviderError('invalid_response', 'ASR service did not return JSON', provider=self.name) from None
                if not isinstance(result, dict) or not isinstance(result.get('text'), str) or len(result['text']) > 4000:
                    raise ProviderError('invalid_response', 'ASR response must contain a text string of at most 4000 characters', provider=self.name)
                return result['text'], redact(result, secrets=self.secrets)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _error(exc, self.name, self.secrets) from None

    async def aclose(self):
        if self._closing:
            return
        self._closing = True
        await asyncio.gather(*(connection.aclose() for connection in tuple(self._connections)))
        if self._owns_client:
            await self.client.aclose()


class VLLMASRStream:
    def __init__(self, provider, request_id):
        self.provider, self.request_id = provider, request_id
        self._started = time.perf_counter()
        self._pcm = bytearray()
        self._finished = asyncio.Event()
        self._closed = False
        self._reading = False
        self._request = None
        self.metrics = {'request_id': request_id, 'audio_bytes': 0, 'audio_ms': 0,
                        'first_partial_ms': None, 'final_ms': None, 'partial_results': False}

    def _elapsed(self):
        return (time.perf_counter() - self._started) * 1000

    async def send_audio(self, pcm):
        try:
            self.provider.input_format.validate_chunk(pcm, max_duration_ms=200)
        except ValueError as exc:
            raise _error(exc, self.provider.name) from None
        if self._closed or self._finished.is_set():
            raise ProviderError('invalid_request', 'ASR input has already ended', provider=self.provider.name)
        if self._elapsed() >= self.provider.max_record_seconds * 1000 or len(self._pcm) + len(pcm) > self.provider.max_audio_bytes:
            raise ProviderError('invalid_request', 'ASR utterance exceeds its audio/time budget', provider=self.provider.name)
        self._pcm.extend(pcm)
        self.metrics.update(audio_bytes=len(self._pcm), audio_ms=self.provider.input_format.duration_ms(len(self._pcm)))

    async def finish(self):
        if self._finished.is_set():
            return
        if self._closed:
            raise ProviderError('invalid_request', 'ASR stream is closed', provider=self.provider.name)
        self.metrics['stop_sent_ms'] = self._elapsed()
        self._finished.set()

    async def results(self):
        if self._reading or self._closed:
            raise ProviderError('invalid_request', 'ASR results require one active consumer', provider=self.provider.name)
        self._reading = True
        try:
            await self._finished.wait()
            if self._closed:
                return
            if not self._pcm:
                raise ProviderError('invalid_request', 'ASR input contains no audio', provider=self.provider.name)
            self.metrics['upload_started_ms'] = self._elapsed()
            self._request = asyncio.create_task(self.provider._transcribe(self.request_id, bytes(self._pcm)))
            text, trace = await self._request
            if self._closed:
                return
            self.metrics.update(final_ms=self._elapsed(), provider_response=trace)
            yield {'type': 'final', 'text': text, 'metrics': dict(self.metrics)}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _error(exc, self.provider.name, self.provider.secrets) from None
        finally:
            await self.aclose()

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        self._finished.set()
        if self._request and self._request is not asyncio.current_task() and not self._request.done():
            self._request.cancel()
            await asyncio.gather(self._request, return_exceptions=True)
        self._pcm.clear()
        self.provider._connections.discard(self)


class VolcengineTTSProvider:
    name = 'volcengine'
    output_format = PCM24K_TTS

    def __init__(self, adapter=None, *, profile=None):
        from backend.tts import VolcanoTTS, XIAOHE_VOICE
        profile = profile or {}
        arguments = {key: value for key, value in {
            'api_key': _secret(profile, 'key_env'), 'app_id': _secret(profile, 'app_id_env'),
            'access_token': _secret(profile, 'access_token_env'), 'timeout_seconds': profile.get('timeout_seconds'),
        }.items() if value is not None}
        self.voices = {'assistant': XIAOHE_VOICE, 'default': XIAOHE_VOICE, **profile.get('voices', {})}
        if any(not isinstance(key, str) or not isinstance(value, str) or not key or not value for key, value in self.voices.items()):
            raise ValueError('TTS voices must map nonempty logical aliases to provider voice identifiers')
        self.adapter = adapter if adapter is not None else VolcanoTTS(**arguments, allowed_voices={XIAOHE_VOICE, *self.voices.values()})
        self.legacy_voice = XIAOHE_VOICE
        self.secrets = tuple(value for value in (getattr(self.adapter, '_api_key', None), getattr(self.adapter, '_app_id', None),
                                                getattr(self.adapter, '_access_token', None)) if value)
        self._closing = False

    def _voice(self, voice):
        if voice in (None, ''):
            voice = 'assistant'
        if voice in self.voices:
            return self.voices[voice]
        # Already persisted personas can retain their original provider voice ID.
        if voice in (self.legacy_voice, 'xiaohe2.0', '小何2.0'):
            return self.legacy_voice
        raise ProviderError('unsupported_capability', 'Unknown logical TTS voice alias', provider=self.name)

    def info(self):
        return {**self.adapter.info(), 'provider': self.name, 'execution_location': 'cloud', 'voice': 'assistant',
                'voice_aliases': list(self.voices), 'output_format': self.output_format.to_dict(),
                'capabilities': {'streaming': True, 'alignment': True}}

    def safe_message(self, value):
        return str(redact(str(value), secrets=self.secrets))

    def _configured(self):
        if self._closing:
            raise ProviderError('unavailable', 'TTS provider is closed', provider=self.name)
        if not self.info().get('configured'):
            raise ProviderError('not_configured', 'TTS credentials or resource configuration are missing', provider=self.name,
                                details={'status': self.info()})

    async def stream_synthesize(self, texts, on_event=None, voice=None):
        self._configured()
        audio_bytes, sentence_start = 0, 0.0
        stream = self.adapter.stream_synthesize(texts, voice=self._voice(voice))
        try:
            async for item in stream:
                if item.get('type') == 'audio':
                    audio = item.get('audio')
                    try:
                        self.output_format.validate_chunk(audio)
                    except ValueError:
                        raise ProviderError('invalid_response', 'TTS provider returned an invalid PCM frame', provider=self.name) from None
                    if any(key in item and item[key] != value for key, value in self.output_format.to_dict().items()):
                        raise ProviderError('invalid_response', 'TTS provider returned an incompatible audio format', provider=self.name)
                    # Native provider fields are diagnostics, never the authoritative wire format.
                    normalized = {**redact({key: value for key, value in item.items() if key != 'audio'}, secrets=self.secrets),
                                  'type': 'audio', 'audio': audio, **self.output_format.to_dict()}
                    audio_bytes += len(audio)
                elif item.get('event') == 'sentence.started':
                    sentence_start = self.output_format.duration_ms(audio_bytes)
                    normalized = {'type': 'diagnostic', 'provider': self.name, 'provider_event': redact(item, secrets=self.secrets)}
                elif item.get('event') == 'sentence.finished':
                    data = item.get('data') or {}
                    if isinstance(data.get('text'), str) and data['text'] and self.output_format.duration_ms(audio_bytes) > sentence_start:
                        yield {'type': 'alignment', 'text': data['text'], 'start_ms': sentence_start,
                               'end_ms': self.output_format.duration_ms(audio_bytes), 'precision': 'provider'}
                    normalized = {'type': 'diagnostic', 'provider': self.name, 'provider_event': redact(item, secrets=self.secrets)}
                else:
                    normalized = {'type': 'diagnostic', 'provider': self.name, 'provider_event': redact(item, secrets=self.secrets)}
                if on_event is not None:
                    result = on_event(normalized)
                    if inspect.isawaitable(result):
                        await result
                yield normalized
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _error(exc, self.name, self.secrets) from None
        finally:
            await stream.aclose()

    async def synthesize_with_metrics(self, text, voice=None):
        self._configured()
        try:
            return await self.adapter.synthesize_with_metrics(text, voice=self._voice(voice))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _error(exc, self.name, self.secrets) from None

    async def synthesize(self, text, voice=None):
        return (await self.synthesize_with_metrics(text, voice))['audio']

    async def prewarm_stream(self):
        self._configured()
        try:
            return await self.adapter.prewarm_stream()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _error(exc, self.name, self.secrets) from None

    async def aclose(self):
        if self._closing:
            return
        self._closing = True
        await self.adapter.aclose()


def build_asr(profile):
    provider = profile.get('provider', 'volcengine')
    if provider == 'volcengine':
        return VolcengineASRProvider(profile=profile)
    if provider == 'vllm':
        return VLLMASRProvider(profile)
    if provider == 'elevenlabs':
        from .elevenlabs_audio import ElevenLabsASRProvider
        return ElevenLabsASRProvider(profile)
    raise ValueError('Unsupported ASR provider: ' + str(provider))


def build_tts(profile):
    if profile.get('provider') == 'elevenlabs':
        from .elevenlabs_tts import ElevenLabsTTSProvider
        return ElevenLabsTTSProvider(profile)
    if profile.get('provider') == 'google':
        from .google_audio import GoogleTTSProvider
        return GoogleTTSProvider(profile)
    if profile.get('provider', 'volcengine') != 'volcengine':
        raise ValueError('Unsupported TTS provider: ' + str(profile.get('provider')))
    return VolcengineTTSProvider(profile=profile)
