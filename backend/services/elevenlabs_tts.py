"""Demo ElevenLabs TTS: bounded per-utterance WebSockets, real PCM24K only."""
from __future__ import annotations

import asyncio
import base64
from contextlib import aclosing
import inspect
import io
import json
import math
import os
import sys
import time
from urllib.parse import urlencode
import wave

from websockets.exceptions import ConnectionClosed

from .audio import PCM24K_TTS
from .contracts import ProviderError, redact


DEFAULT_VOICE = 'JBFqnCBsd6RMkjVDRZzb'
LEGACY_DEFAULT_VOICES = {'zh_female_xiaohe_uranus_bigtts', 'xiaohe2.0', '小何2.0'}
MAX_TEXT_BYTES = 20000
MAX_CHUNK_BYTES = 5000
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
ERROR_CODES = {'invalid_api_key': 'auth', 'unauthorized': 'auth',
               'missing_permissions': 'permission_denied', 'insufficient_credits': 'quota_exceeded',
               'quota_exceeded': 'quota_exceeded', 'voice_not_found': 'unsupported_capability',
               'model_not_found': 'unsupported_capability', 'unsupported_output_format': 'unsupported_capability',
               'invalid_output_format': 'unsupported_capability', 'invalid_request': 'invalid_request',
               'rate_limit_exceeded': 'rate_limit', 'rate_limited': 'rate_limit'}


class ElevenLabsTTSProvider:
    name = 'elevenlabs'
    output_format = PCM24K_TTS

    def __init__(self, profile, *, connect=None):
        if any(key in profile for key in ('base_url', 'base_url_env', 'url', 'url_env')):
            raise ValueError('ElevenLabs TTS uses the fixed official endpoint')
        self.model = profile.get('model', 'eleven_flash_v2_5')
        self.language = profile.get('language', 'zh')
        if not isinstance(self.model, str) or not self.model or any(
                c not in 'abcdefghijklmnopqrstuvwxyz0123456789_' for c in self.model):
            raise ValueError('ElevenLabs TTS model must be an explicit model identifier')
        if not isinstance(self.language, str) or len(self.language) != 2 or any(
                c not in 'abcdefghijklmnopqrstuvwxyz' for c in self.language):
            raise ValueError('ElevenLabs TTS language must be a two-letter code')
        key_env = profile.get('key_env', 'ELEVENLABS_API_KEY')
        if not isinstance(key_env, str) or not key_env or any(
                c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_' for c in key_env):
            raise ValueError('ElevenLabs TTS key_env must name an environment variable')
        self._key = os.getenv(key_env, '')
        self.secrets = (self._key,) if self._key else ()
        self.timeout_seconds = profile.get('timeout_seconds', 20)
        if (type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= 300):
            raise ValueError('ElevenLabs TTS timeout must be positive and at most 300 seconds')
        aliases = profile.get('voices', {})
        if not isinstance(aliases, dict) or any(not isinstance(k, str) or not k or
                not isinstance(v, str) or not v or len(v) > 100 or not v.isascii() or not v.isalnum()
                for k, v in aliases.items()):
            raise ValueError('TTS voices must map logical aliases to provider voice identifiers')
        default = aliases.get('default', aliases.get('assistant', DEFAULT_VOICE))
        self.voices = {'default': default, 'assistant': default, **aliases}
        self._connect = connect
        self._closed = asyncio.Event()
        self._sockets, self._senders = set(), set()

    def info(self):
        return {'provider': self.name, 'execution_location': 'cloud', 'configured': bool(self._key),
                'missing_fields': [] if self._key else ['api_key'], 'model': self.model,
                'endpoint': 'api.elevenlabs.io', 'routing': 'global', 'language': self.language,
                'voice': 'assistant', 'voice_aliases': list(self.voices),
                'output_format': self.output_format.to_dict(),
                'capabilities': {'streaming': True, 'streaming_input': True, 'alignment': False}}

    def safe_message(self, value):
        return str(redact(str(value), secrets=self.secrets))

    def _check(self):
        if self._closed.is_set():
            raise ProviderError('unavailable', 'TTS provider is closed', provider=self.name)
        if not self._key:
            raise ProviderError('not_configured', 'ElevenLabs TTS API key is missing', provider=self.name)

    def _voice(self, voice):
        if voice in (None, '') or voice in LEGACY_DEFAULT_VOICES:
            voice = 'assistant'
        if voice not in self.voices:
            raise ProviderError('unsupported_capability', 'Unknown logical TTS voice alias', provider=self.name)
        return self.voices[voice]

    async def _wait(self, awaitable, deadline, *, sender=None):
        operation = asyncio.ensure_future(awaitable)
        closed = asyncio.create_task(self._closed.wait())
        try:
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise TimeoutError()
                watched = {operation, closed}
                if sender is not None:
                    watched.add(sender)
                done, _ = await asyncio.wait(watched, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                if closed in done:
                    raise ProviderError('unavailable', 'TTS provider is closed', provider=self.name)
                if sender in done:
                    sender.result()
                    sender = None
                if operation in done:
                    return operation.result()
                if not done:
                    raise TimeoutError()
        finally:
            for task in (operation, closed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation, closed, return_exceptions=True)

    def _error(self, error, metrics=None):
        status = getattr(getattr(error, 'response', None), 'status_code', None)
        code = {401: 'auth', 403: 'permission_denied', 429: 'rate_limit',
                502: 'unavailable', 503: 'unavailable', 504: 'unavailable'}.get(status, 'provider_error')
        details = {'error_type': type(error).__name__, 'http_status': status,
                   'metrics': dict(metrics or {})}
        if isinstance(error, ProviderError):
            code = error.code if error.code in {'auth', 'permission_denied', 'quota_exceeded',
                'unsupported_capability', 'invalid_request', 'rate_limit', 'provider_error',
                'timeout', 'not_configured', 'unavailable', 'invalid_response'} else 'provider_error'
            provider_code = error.details.get('provider_code')
            if isinstance(provider_code, str) and provider_code in ERROR_CODES:
                details['provider_code'] = provider_code
        elif isinstance(error, TimeoutError):
            code = 'timeout'
        elif isinstance(error, ConnectionClosed):
            code = 'invalid_response'
        elif isinstance(error, ModuleNotFoundError):
            code = 'not_configured'
        # Never forward arbitrary WS close reasons, error bodies or input errors.
        return ProviderError(code, 'ElevenLabs streaming TTS failed (' + code + ')', provider=self.name,
                             retryable=code in {'timeout', 'unavailable', 'rate_limit'}, details=details)

    @staticmethod
    def _text(text):
        if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_CHUNK_BYTES:
            raise ProviderError('invalid_request', 'TTS text chunk exceeds its UTF-8 size limit', provider='elevenlabs')
        return text if text.strip() else None

    @staticmethod
    def _message(raw):
        if (not isinstance(raw, (str, bytes))
                or len(raw.encode('utf-8') if isinstance(raw, str) else raw) > MAX_MESSAGE_BYTES):
            raise ProviderError('invalid_response', 'Invalid TTS message size/type', provider='elevenlabs')
        try:
            message = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ProviderError('invalid_response', 'Invalid TTS JSON', provider='elevenlabs') from None
        if not isinstance(message, dict):
            raise ProviderError('invalid_response', 'Invalid TTS response shape', provider='elevenlabs')
        if 'error' in message or 'detail' in message:
            detail = message.get('detail')
            candidates = [message.get('error'), message.get('status'), message.get('code')]
            if isinstance(detail, dict):
                candidates.extend((detail.get('status'), detail.get('code')))
            code = next((value for value in candidates if isinstance(value, str) and value in ERROR_CODES), None)
            raise ProviderError(ERROR_CODES.get(code, 'provider_error'), 'TTS provider rejected synthesis',
                                provider='elevenlabs', details={'provider_code': code})
        for field in ('isFinal', 'is_final'):
            # Audio frames carry null while synthesis is still in progress.
            # Only explicit true below confirms completion; EOF never does.
            if message.get(field) is not None and type(message[field]) is not bool:
                raise ProviderError('invalid_response', 'Invalid TTS completion flag', provider='elevenlabs')
        return message

    async def stream_synthesize(self, texts, on_event=None, voice=None):
        self._check()
        selected_voice = self._voice(voice)
        started = time.perf_counter()
        deadline = started + self.timeout_seconds
        source = aiter(texts)
        websocket = sender = None
        metrics = {'first_packet_ms': None, 'first_text_to_audio_ms': None,
                   'audio_bytes': 0, 'text_chunks': 0, 'alignment_events': 0}

        async def emit(item):
            if on_event is not None:
                result = on_event(item)
                if inspect.isawaitable(result):
                    await result

        try:
            while True:
                try:
                    first = self._text(await self._wait(anext(source), deadline))
                except StopAsyncIteration:
                    return
                if first is not None:
                    break
            first_text_at = time.perf_counter()
            deadline = first_text_at + self.timeout_seconds
            metrics['input_wait_ms'] = (first_text_at - started) * 1000
            connect = self._connect
            if connect is None:
                from websockets.asyncio.client import connect
            query = urlencode({'model_id': self.model, 'language_code': self.language, 'output_format': 'pcm_24000'})
            endpoint = f'wss://api.elevenlabs.io/v1/text-to-speech/{selected_voice}/stream-input?{query}'
            async def open_socket():
                nonlocal websocket
                websocket = await connect(endpoint, additional_headers={'xi-api-key': self._key},
                    open_timeout=min(10, self.timeout_seconds), close_timeout=1,
                    max_size=MAX_MESSAGE_BYTES, compression=None)
                self._sockets.add(websocket)

            await self._wait(open_socket(), deadline)
            metrics['connection_ready_ms'] = (time.perf_counter() - first_text_at) * 1000
            headers = getattr(getattr(websocket, 'response', None), 'headers', {})
            metrics['response_headers'] = {name: self.safe_message(headers[name])[:256]
                for name in ('x-region', 'request-id', 'x-request-id')
                if isinstance(headers.get(name), str)}
            input_complete, text_bytes = False, 0
            sent_text_at = None

            async def send():
                nonlocal input_complete, text_bytes, sent_text_at
                await self._wait(websocket.send(json.dumps({'text': ' ', 'voice_settings': {
                    'stability': .5, 'similarity_boost': .8, 'use_speaker_boost': False}})), deadline)
                text = first
                while True:
                    if text is not None:
                        text_bytes += len(text.encode('utf-8'))
                        if text_bytes > MAX_TEXT_BYTES:
                            raise ProviderError('invalid_request', 'TTS utterance exceeds its text limit', provider=self.name)
                        if sent_text_at is None:
                            sent_text_at = time.perf_counter()
                        # Flush each actual clause instead of waiting for the provider's character buffer.
                        await self._wait(websocket.send(json.dumps({'text': text + ' ', 'flush': True},
                                                                  ensure_ascii=False)), deadline)
                        metrics['text_chunks'] += 1
                    try:
                        text = self._text(await self._wait(anext(source), deadline))
                    except StopAsyncIteration:
                        break
                input_complete = True
                await self._wait(websocket.send(json.dumps({'text': ''})), deadline)

            sender = asyncio.create_task(send())
            self._senders.add(sender)
            pending, prefix_checked = b'', False
            while True:
                message = self._message(await self._wait(websocket.recv(), deadline, sender=sender))
                final = message.get('isFinal') is True or message.get('is_final') is True
                encoded = message.get('audio')
                if encoded is not None:
                    if not isinstance(encoded, str):
                        raise ProviderError('invalid_response', 'Invalid TTS audio payload', provider=self.name)
                    try:
                        pending += base64.b64decode(encoded, validate=True)
                    except ValueError:
                        raise ProviderError('invalid_response', 'Invalid TTS audio encoding', provider=self.name) from None
                if metrics['audio_bytes'] + len(pending) > MAX_AUDIO_BYTES:
                    raise ProviderError('invalid_response', 'TTS audio exceeded its size limit', provider=self.name)
                if not prefix_checked and (len(pending) >= 12 or final):
                    if pending[:4] == b'RIFF' and pending[8:12] == b'WAVE':
                        raise ProviderError('invalid_response', 'Expected raw PCM, received a WAV container', provider=self.name)
                    prefix_checked = True
                size = len(pending) - len(pending) % 2 if prefix_checked else 0
                if size:
                    audio, pending = pending[:size], pending[size:]
                    metrics['audio_bytes'] += len(audio)
                    if metrics['first_packet_ms'] is None:
                        metrics['first_packet_ms'] = (time.perf_counter() - started) * 1000
                        metrics['first_text_to_audio_ms'] = (time.perf_counter() - first_text_at) * 1000
                        metrics['first_sent_text_to_audio_ms'] = (time.perf_counter() - sent_text_at) * 1000 if sent_text_at else None
                    item = {'type': 'audio', 'audio': audio, **self.output_format.to_dict()}
                    await emit(item)
                    yield item
                alignment = {key: message[key] for key in ('alignment', 'normalizedAlignment', 'normalized_alignment')
                             if message.get(key) is not None}
                if alignment:
                    metrics['alignment_events'] += 1
                    item = {'type': 'diagnostic', 'provider': self.name,
                            'provider_event': redact(alignment, secrets=self.secrets)}
                    await emit(item)
                    yield item
                if final:
                    if not input_complete:
                        raise ProviderError('invalid_response', 'TTS ended before text input completed', provider=self.name)
                    break
            await self._wait(asyncio.shield(sender), deadline)
            if pending or not metrics['audio_bytes']:
                raise ProviderError('invalid_response', 'TTS returned incomplete or empty PCM', provider=self.name)
            metrics.update(total_ms=(time.perf_counter() - started) * 1000,
                           audio_ms=self.output_format.duration_ms(metrics['audio_bytes']))
            item = {'type': 'metrics', 'provider': self.name, **metrics}
            await emit(item)
            yield item
        except asyncio.CancelledError:
            raise
        except Exception as error:
            metrics['total_ms'] = (time.perf_counter() - started) * 1000
            raise self._error(error, metrics) from None
        finally:
            active_error = sys.exc_info()[1]
            if sender is not None:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
                self._senders.discard(sender)
            cleanups = []
            if websocket is not None:
                self._sockets.discard(websocket)
                cleanups.append(websocket.close)
            close = getattr(source, 'aclose', None)
            if close is not None:
                cleanups.append(close)
            cleanup_error = None
            for close in cleanups:
                try:
                    await asyncio.wait_for(close(), min(1, self.timeout_seconds))
                except asyncio.CancelledError:
                    if not isinstance(active_error, (asyncio.CancelledError, GeneratorExit)):
                        raise
                except Exception as error:
                    cleanup_error = error
            if cleanup_error is not None and active_error is None:
                raise self._error(cleanup_error, metrics) from None

    async def synthesize_with_metrics(self, text, voice=None):
        if not self._text(text):
            raise ProviderError('invalid_request', 'TTS text cannot be empty', provider=self.name)

        async def texts():
            yield text

        pcm, metrics = bytearray(), {}
        async with aclosing(self.stream_synthesize(texts(), voice=voice)) as stream:
            async for item in stream:
                if item['type'] == 'audio':
                    pcm.extend(item['audio'])
                elif item['type'] == 'metrics':
                    metrics = {key: value for key, value in item.items() if key != 'type'}
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(pcm)
        return {'audio': output.getvalue(), 'content_type': 'audio/wav', **metrics}

    async def synthesize(self, text, voice=None):
        return (await self.synthesize_with_metrics(text, voice))['audio']

    async def prewarm_stream(self):
        self._check()
        # A single-context WS belongs to one voice/utterance. A discarded WS
        # does not warm the next one, and filler synthesis would be misleading.
        return {'ok': True, 'provider': self.name, 'prewarmed': False, 'synthesis_requests': 0,
                'reason': 'per_utterance_connection', 'total_ms': 0}

    async def aclose(self):
        if self._closed.is_set():
            return
        self._closed.set()
        for sender in tuple(self._senders):
            sender.cancel()
        await asyncio.gather(*self._senders, return_exceptions=True)
        for websocket in tuple(self._sockets):
            try:
                await asyncio.wait_for(websocket.close(), min(1, self.timeout_seconds))
            except Exception:
                pass
