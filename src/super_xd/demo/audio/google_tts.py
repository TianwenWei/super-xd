"""Demo Google Chirp 3 streaming TTS; no SDK imports enter the NLP runtime."""
from __future__ import annotations

import asyncio
from contextlib import aclosing
import inspect
import io
import math
import sys
import time
import wave

from super_xd.demo.audio.providers import PCM24K_TTS
from super_xd.nlp.config.services import resolved
from super_xd.nlp.contracts.providers import ProviderError, redact
from super_xd.nlp.adapters.models.google_auth import GoogleADCAuth


ENDPOINTS = {'eu': 'eu-texttospeech.googleapis.com:443',
             'us': 'us-texttospeech.googleapis.com:443', 'global': 'texttospeech.googleapis.com:443'}
LEGACY_DEFAULT_VOICES = {'zh_female_xiaohe_uranus_bigtts', 'xiaohe2.0', '小何2.0'}
MAX_CHUNK_BYTES = 5000
MAX_AUDIO_BYTES = 20 * 1024 * 1024


class _GoogleStream:
    def __init__(self, call, types, voice, language):
        self.call, self.types, self.voice, self.language = call, types, voice, language

    async def configure(self):
        t = self.types
        await self.call.write(t.StreamingSynthesizeRequest(streaming_config=t.StreamingSynthesizeConfig(
            voice=t.VoiceSelectionParams(language_code=self.language, name=self.voice),
            streaming_audio_config=t.StreamingAudioConfig(audio_encoding=t.AudioEncoding.PCM,
                                                         sample_rate_hertz=24000))))

    async def send_text(self, text):
        await self.call.write(self.types.StreamingSynthesizeRequest(
            input=self.types.StreamingSynthesisInput(text=text)))

    async def finish(self):
        await self.call.done_writing()

    async def receive(self):
        import grpc
        item = await self.call.read()
        return None if item is grpc.aio.EOF else bytes(item.audio_content)

    def cancel(self):
        self.call.cancel()


class _GoogleTransport:
    def __init__(self, endpoint):
        import grpc
        from google.cloud import texttospeech_v1
        self.types = texttospeech_v1
        self.channel = grpc.aio.secure_channel(endpoint, grpc.ssl_channel_credentials(),
                                              options=(('grpc.enable_retries', 0),))
        self.rpc = self.channel.stream_stream('/google.cloud.texttospeech.v1.TextToSpeech/StreamingSynthesize',
            request_serializer=self.types.StreamingSynthesizeRequest.serialize,
            response_deserializer=self.types.StreamingSynthesizeResponse.deserialize)

    async def ready(self):
        await self.channel.channel_ready()

    def start(self, *, voice, language, metadata, timeout):
        return _GoogleStream(self.rpc(timeout=timeout, metadata=metadata, wait_for_ready=False),
                             self.types, voice, language)

    async def aclose(self):
        await self.channel.close(grace=None)


class GoogleTTSProvider:
    name = 'google'
    output_format = PCM24K_TTS

    def __init__(self, profile, *, transport=None, auth=None):
        self.project = resolved(profile, 'project')
        self.location = profile.get('location', 'eu')
        if self.location not in ENDPOINTS or profile.get('base_url') or profile.get('base_url_env'):
            raise ValueError('Google TTS requires a supported location without a custom endpoint')
        self.endpoint = ENDPOINTS[self.location]
        self.language = profile.get('language', 'cmn-CN')
        if not isinstance(self.language, str) or not self.language or any(
                not part.isascii() or not part.isalnum() for part in self.language.split('-')):
            raise ValueError('Google TTS language must be a language code')
        self.timeout_seconds = profile.get('timeout_seconds', 20)
        if type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError('Google TTS timeout must be positive and finite')
        aliases = profile.get('voices', {})
        if not isinstance(aliases, dict) or any(not isinstance(k, str) or not k or
                not isinstance(v, str) or not v for k, v in aliases.items()):
            raise ValueError('TTS voices must map nonempty logical aliases to voice identifiers')
        default_voice = aliases.get('default', aliases.get('assistant', 'Kore'))
        self.voices = {'default': default_voice, 'assistant': default_voice, **aliases}
        prefix = self.language + '-Chirp3-HD-'
        for alias, voice in self.voices.items():
            suffix = voice.removeprefix(prefix)
            if not suffix.isascii() or not suffix.isalpha():
                raise ValueError('Google TTS voice must be a Chirp 3 HD name for the configured language')
            self.voices[alias] = prefix + suffix
        self.auth = auth if auth is not None else GoogleADCAuth(self.project)
        self._transport, self._owns_transport = transport, transport is None
        self._closed = asyncio.Event()
        self._calls, self._senders = set(), set()
        self._auth_verified = False

    @property
    def secrets(self):
        return self.auth.secrets

    def safe_message(self, value):
        return str(redact(str(value), secrets=self.secrets))

    def info(self):
        return {'provider': self.name, 'execution_location': 'cloud', 'configured': bool(self.project),
                'missing_fields': [] if self.project else ['project'], 'authentication': 'adc',
                'authentication_verified': self._auth_verified, 'location': self.location,
                'endpoint': self.endpoint, 'language': self.language, 'voice': 'assistant',
                'voice_aliases': list(self.voices), 'output_format': self.output_format.to_dict(),
                'capabilities': {'streaming': True, 'streaming_input': True, 'alignment': False}}

    def _check(self):
        if self._closed.is_set():
            raise ProviderError('unavailable', 'TTS provider is closed', provider=self.name)
        if not self.project:
            raise ProviderError('not_configured', 'Google TTS project is missing', provider=self.name)

    def _voice(self, voice):
        # Existing Demo personas used the former fixed voice as their default.
        # This migration maps only those defaults, never arbitrary provider IDs.
        if voice in (None, '') or voice in LEGACY_DEFAULT_VOICES:
            voice = 'assistant'
        if voice not in self.voices:
            raise ProviderError('unsupported_capability', 'Unknown logical TTS voice alias', provider=self.name)
        return self.voices[voice]

    async def _wait(self, awaitable, deadline):
        operation = asyncio.ensure_future(awaitable)
        closed = asyncio.create_task(self._closed.wait())
        try:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError()
            done, _ = await asyncio.wait((operation, closed), timeout=remaining,
                                         return_when=asyncio.FIRST_COMPLETED)
            if closed in done:
                raise ProviderError('unavailable', 'TTS provider is closed', provider=self.name)
            if operation not in done:
                raise TimeoutError()
            return operation.result()
        finally:
            for task in (operation, closed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation, closed, return_exceptions=True)

    async def _prepare(self, deadline):
        started = time.perf_counter()
        headers = await self._wait(self.auth.request_headers(), deadline)
        self._auth_verified = True
        auth_ms = (time.perf_counter() - started) * 1000
        self._check()
        if self._transport is None:
            self._transport = _GoogleTransport(self.endpoint)
        started = time.perf_counter()
        await self._wait(self._transport.ready(), deadline)
        return tuple((key.lower(), value) for key, value in headers.items()), {
            'auth_ms': auth_ms, 'connection_ready_ms': (time.perf_counter() - started) * 1000}

    def _error(self, error, metrics=None):
        code = getattr(error, 'code', None)
        if callable(code):
            code = code()
        status = getattr(code, 'name', None)
        mapped = {'DEADLINE_EXCEEDED': 'timeout', 'UNAUTHENTICATED': 'auth',
                  'PERMISSION_DENIED': 'permission_denied', 'RESOURCE_EXHAUSTED': 'rate_limit',
                  'UNAVAILABLE': 'unavailable', 'INVALID_ARGUMENT': 'invalid_request',
                  'NOT_FOUND': 'unsupported_capability'}.get(status, 'provider_error')
        if isinstance(error, ProviderError):
            mapped = error.code
        elif isinstance(error, TimeoutError):
            mapped = 'timeout'
        elif isinstance(error, ModuleNotFoundError):
            mapped = 'not_configured'
        # gRPC/ADC error text may contain credentials or request metadata.
        return ProviderError(mapped, 'Google streaming TTS failed (' + mapped + ')', provider=self.name,
            retryable=mapped in {'timeout', 'unavailable', 'rate_limit'},
            details={'error_type': type(error).__name__, 'grpc_code': status, 'metrics': dict(metrics or {})})

    @staticmethod
    def _text(text):
        if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_CHUNK_BYTES:
            raise ProviderError('invalid_request', 'TTS text chunk must contain at most 5000 UTF-8 bytes', provider='google')
        return text if text.strip() else None

    async def stream_synthesize(self, texts, on_event=None, voice=None):
        self._check()
        selected_voice = self._voice(voice)
        started = time.perf_counter()
        deadline = started + self.timeout_seconds
        source = aiter(texts)
        call, sender = None, None
        metrics = {'first_packet_ms': None, 'first_text_to_audio_ms': None, 'audio_bytes': 0, 'text_chunks': 0}
        try:
            while True:
                try:
                    first = self._text(await self._wait(anext(source), deadline))
                except StopAsyncIteration:
                    return
                if first is not None:
                    break
            first_text_at = time.perf_counter()
            # Input waiting is separately bounded; synthesis gets its own fixed
            # deadline from the first clause, rather than consuming LLM latency.
            deadline = first_text_at + self.timeout_seconds
            metadata, preparation = await self._prepare(deadline)
            metrics.update(preparation, input_wait_ms=(first_text_at - started) * 1000)
            self._check()
            call = self._transport.start(voice=selected_voice, language=self.language,
                                          metadata=metadata, timeout=max(.001, deadline - time.perf_counter()))
            self._calls.add(call)
            input_complete = False

            async def send():
                nonlocal input_complete
                try:
                    await self._wait(call.configure(), deadline)
                    await self._wait(call.send_text(first), deadline)
                    metrics['text_chunks'] += 1
                    while True:
                        try:
                            text = self._text(await self._wait(anext(source), deadline))
                        except StopAsyncIteration:
                            break
                        if text is not None:
                            await self._wait(call.send_text(text), deadline)
                            metrics['text_chunks'] += 1
                    input_complete = True
                    await self._wait(call.finish(), deadline)
                except BaseException:
                    call.cancel()
                    raise

            sender = asyncio.create_task(send())
            self._senders.add(sender)
            pending = b''
            while True:
                chunk = await self._wait(call.receive(), deadline)
                if chunk is None:
                    if not input_complete:
                        raise ProviderError('invalid_response', 'TTS stream ended before its text input', provider=self.name)
                    break
                if not isinstance(chunk, bytes):
                    raise ProviderError('invalid_response', 'TTS returned invalid PCM data', provider=self.name)
                pending += chunk
                size = len(pending) - len(pending) % 2
                if not size:
                    continue
                audio, pending = pending[:size], pending[size:]
                metrics['audio_bytes'] += len(audio)
                if metrics['audio_bytes'] > MAX_AUDIO_BYTES:
                    raise ProviderError('invalid_response', 'TTS audio exceeded its size limit', provider=self.name)
                if metrics['first_packet_ms'] is None:
                    metrics['first_packet_ms'] = (time.perf_counter() - started) * 1000
                    metrics['first_text_to_audio_ms'] = (time.perf_counter() - first_text_at) * 1000
                item = {'type': 'audio', 'audio': audio, **self.output_format.to_dict()}
                if on_event is not None:
                    result = on_event(item)
                    if inspect.isawaitable(result):
                        await result
                yield item
            await self._wait(asyncio.shield(sender), deadline)
            if pending or not metrics['audio_bytes']:
                raise ProviderError('invalid_response', 'TTS returned incomplete or empty PCM audio', provider=self.name)
            metrics.update(total_ms=(time.perf_counter() - started) * 1000,
                           audio_ms=self.output_format.duration_ms(metrics['audio_bytes']))
            item = {'type': 'metrics', 'provider': self.name, **metrics}
            if on_event is not None:
                result = on_event(item)
                if inspect.isawaitable(result):
                    await result
            yield item
        except asyncio.CancelledError:
            # A failed input producer cancels the gRPC call too. Preserve that
            # failure instead of reporting it as a user-initiated cancellation.
            if (not asyncio.current_task().cancelling() and sender is not None and sender.done()
                    and not sender.cancelled() and sender.exception() is not None):
                raise self._error(sender.exception(), metrics) from None
            raise
        except Exception as caught_error:
            failure = caught_error
            if sender is not None and sender.done() and not sender.cancelled() and sender.exception() is not None:
                failure = sender.exception()
            metrics['total_ms'] = (time.perf_counter() - started) * 1000
            raise self._error(failure, metrics) from None
        finally:
            active_error = sys.exc_info()[1]
            if call is not None:
                call.cancel()
                self._calls.discard(call)
            if sender is not None:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
                self._senders.discard(sender)
            close = getattr(source, 'aclose', None)
            if close is not None:
                try:
                    await asyncio.wait_for(close(), min(1, self.timeout_seconds))
                except asyncio.CancelledError:
                    if not isinstance(active_error, (asyncio.CancelledError, GeneratorExit)):
                        raise
                except Exception as error:
                    if active_error is None:
                        raise self._error(error, metrics) from None
                    if isinstance(active_error, ProviderError):
                        active_error.details['input_close_error_type'] = type(error).__name__

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
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(pcm)
        return {'audio': buffer.getvalue(), 'content_type': 'audio/wav', **metrics}

    async def synthesize(self, text, voice=None):
        return (await self.synthesize_with_metrics(text, voice))['audio']

    async def prewarm_stream(self):
        self._check()
        started = time.perf_counter()
        try:
            _, metrics = await self._prepare(started + self.timeout_seconds)
            return {'ok': True, 'provider': self.name, 'synthesis_requests': 0, **metrics,
                    'total_ms': (time.perf_counter() - started) * 1000}
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise self._error(error) from None

    async def aclose(self):
        if self._closed.is_set():
            return
        self._closed.set()
        for call in tuple(self._calls):
            call.cancel()
        for task in tuple(self._senders):
            task.cancel()
        await asyncio.gather(*self._senders, return_exceptions=True)
        if self._transport is not None and self._owns_transport:
            await self._transport.aclose()
