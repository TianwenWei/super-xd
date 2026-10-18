"""Demo Scribe realtime ASR; committed segments become one finalized utterance.

Manual mode still auto-commits after roughly 36s. Commit every 20s of PCM and
wait for that segment before sending more, so finish cannot mistake a delayed
long-buffer commit for acknowledgement of the last audio. No silence is added.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from urllib.parse import urlencode

from super_xd.demo.audio.providers import PCM16K_ASR, _positive, _secret
from super_xd.nlp.contracts.providers import ProviderError, redact
from super_xd.nlp.textutil import is_identifier

_ENDPOINT = 'wss://api.elevenlabs.io/v1/speech-to-text/realtime'
_COMMIT_BYTES = 16000 * 2 * 20
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_TEXT = 4000
_ERROR_CODES = {
    'auth_error': 'auth', 'unaccepted_terms': 'auth', 'quota_exceeded': 'rate_limit',
    'rate_limited': 'rate_limit', 'resource_exhausted': 'unavailable',
    'transcriber_error': 'unavailable', 'queue_overflow': 'unavailable',
    'input_error': 'invalid_request', 'invalid_request': 'invalid_request',
    'commit_throttled': 'invalid_request', 'chunk_size_exceeded': 'invalid_request',
    'insufficient_audio_activity': 'invalid_request', 'session_time_limit_exceeded': 'timeout',
    'error': 'provider_error',
}


def _error(code, *, exception=None, event=None):
    details = {}
    if exception is not None:
        details['exception_type'] = type(exception).__name__
    if event in _ERROR_CODES:
        details['provider_event'] = event
    return ProviderError(code, 'Speech recognition could not complete', provider='elevenlabs',
                         retryable=code in {'timeout', 'rate_limit', 'unavailable'}, details=details)


def _transport_error(exc):
    if isinstance(exc, ProviderError):
        return exc
    status = getattr(getattr(exc, 'response', None), 'status_code', None)
    if status in (401, 403):
        code = 'auth'
    elif status == 429:
        code = 'rate_limit'
    elif isinstance(exc, TimeoutError):
        code = 'timeout'
    else:
        code = 'unavailable'
    error = _error(code, exception=exc)
    if isinstance(status, int):
        error.details['status_code'] = status
    # Provider exception strings and WS close reasons may contain credentials.
    return error


def _decode(raw):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > _MAX_RESPONSE_BYTES:
        raise _error('invalid_response')
    try:
        event = json.loads(raw)
    except (ValueError, UnicodeError):
        raise _error('invalid_response') from None
    if not isinstance(event, dict) or not isinstance(event.get('message_type'), str):
        raise _error('invalid_response')
    kind = event['message_type']
    if kind in _ERROR_CODES:
        raise _error(_ERROR_CODES[kind], event=kind)
    return event


class ElevenLabsASRProvider:
    name = 'elevenlabs'
    input_format = PCM16K_ASR

    def __init__(self, profile, *, ws_connect=None):
        self.model = profile.get('model', 'scribe_v2_realtime')
        self.language = profile.get('language', 'zh')
        if self.model != 'scribe_v2_realtime':
            raise ValueError('ElevenLabs ASR requires scribe_v2_realtime')
        if (not isinstance(self.language, str) or len(self.language) not in (2, 3)
                or not self.language.isascii() or not self.language.isalpha()):
            raise ValueError('ASR language must be an ISO 639 language code')
        if any(profile.get(field) for field in ('base_url', 'endpoint', 'url')):
            raise ValueError('ElevenLabs ASR uses its fixed official endpoint')
        self.key_env = profile.get('key_env', 'ELEVENLABS_API_KEY')
        self.key = _secret({'key_env': self.key_env}, 'key_env') or ''
        if '\r' in self.key or '\n' in self.key:
            raise ValueError('Invalid ASR API key format')
        self.secrets = (self.key,) if self.key else ()
        self.timeout_seconds = _positive(profile, 'timeout_seconds', 15)
        self.max_record_seconds = _positive(profile, 'max_record_seconds', 60)
        if self.max_record_seconds > 60:
            raise ValueError('ASR recording cannot exceed 60 seconds')
        self.max_audio_bytes = int(self.max_record_seconds * 32000)
        self._ws_connect = ws_connect
        self._connections = set()
        self._closing = False

    def info(self):
        return {'provider': self.name, 'configured': bool(self.key), 'execution_location': 'cloud',
                'model': self.model, 'language': self.language,
                'missing_fields': [] if self.key else [self.key_env],
                **self.input_format.to_dict(), 'input_format': self.input_format.to_dict(),
                'capabilities': {'partial_results': True, 'streaming_input': True, 'utterance_only': True}}

    def safe_message(self, value):
        return str(redact(str(value), secrets=self.secrets))

    async def open(self, request_id):
        if self._closing:
            raise _error('unavailable')
        if not is_identifier(request_id, max_length=128, extra='_.:-', alnum_first=True):
            raise _error('invalid_request')
        if not self.key:
            raise _error('not_configured')
        stream = ElevenLabsASRStream(self, request_id)
        self._connections.add(stream)
        try:
            await stream.connect()
            return stream
        except asyncio.CancelledError:
            await stream.aclose()
            raise
        except Exception as exc:
            await stream.aclose()
            raise _transport_error(exc) from None

    async def aclose(self):
        self._closing = True
        await asyncio.gather(*(stream.aclose() for stream in tuple(self._connections)))


class ElevenLabsASRStream:
    def __init__(self, provider, request_id):
        self.provider = provider
        self._started = time.perf_counter()
        self._deadline = self._started + provider.max_record_seconds + provider.timeout_seconds
        self._ws = self._reader = self._connecting = None
        self._closed = self._reading = self._finished = self._finalizing = False
        self._send_lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._commit_changed = asyncio.Event()
        self._closed_event = asyncio.Event()
        self._error = None
        self._committed = self._partial = ''
        self._partial_version = 0
        self._commit_count = self._uncommitted_bytes = 0
        self._pending_commit = False
        self._final = None
        self.metrics = {'request_id': request_id, 'audio_bytes': 0, 'audio_ms': 0,
                        'ws_connect_ms': None, 'session_started_ms': None,
                        'first_partial_ms': None, 'stop_sent_ms': None, 'final_ms': None,
                        'stop_to_final_ms': None, 'committed_segments': 0}

    def _elapsed(self):
        return (time.perf_counter() - self._started) * 1000

    def _check(self):
        if self._error is not None:
            raise self._error
        if self._closed:
            raise _error('invalid_request')

    async def _wait(self, operation, *, remaining=None):
        limit = min(self.provider.timeout_seconds if remaining is None else remaining,
                    self._deadline - time.perf_counter())
        task = asyncio.ensure_future(operation)
        closed = asyncio.create_task(self._closed_event.wait())
        try:
            if limit <= 0:
                raise TimeoutError
            done, _ = await asyncio.wait({task, closed}, timeout=limit,
                                        return_when=asyncio.FIRST_COMPLETED)
            if closed in done:
                self._check()
            if task not in done:
                raise TimeoutError
            return task.result()
        finally:
            for pending in (task, closed):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, closed, return_exceptions=True)

    async def connect(self):
        connect = self.provider._ws_connect
        if connect is None:
            from websockets.asyncio.client import connect
        url = _ENDPOINT + '?' + urlencode({'model_id': self.provider.model,
            'audio_format': 'pcm_16000', 'language_code': self.provider.language,
            'commit_strategy': 'manual'})
        self._connecting = asyncio.ensure_future(connect(url,
            additional_headers={'xi-api-key': self.provider.key},
            open_timeout=self.provider.timeout_seconds, close_timeout=1,
            max_size=_MAX_RESPONSE_BYTES, max_queue=16))
        self._ws = await self._wait(self._connecting)
        self._check()
        self.metrics['ws_connect_ms'] = self._elapsed()
        event = _decode(await self._wait(self._ws.recv()))
        if event['message_type'] != 'session_started':
            raise _error('invalid_response')
        config = event.get('config', {})
        if not isinstance(config, dict) or any(key in config and config[key] != value for key, value in {
                'model_id': self.provider.model, 'audio_format': 'pcm_16000', 'sample_rate': 16000,
                'commit_strategy': 'manual'}.items()):
            raise _error('invalid_response')
        self.metrics['session_started_ms'] = self._elapsed()
        self._reader = asyncio.create_task(self._receive())

    async def _send(self, pcm, *, commit=False):
        await self._wait(self._ws.send(json.dumps({'message_type': 'input_audio_chunk',
            'audio_base_64': base64.b64encode(pcm).decode('ascii'), 'sample_rate': 16000,
            'commit': commit})))

    async def _commit(self):
        # The official SDK sends this empty audio message for manual commit.
        count = self._commit_count
        self._pending_commit = True
        self._commit_changed.clear()
        await self._send(b'', commit=True)
        deadline = time.perf_counter() + self.provider.timeout_seconds
        while self._commit_count == count:
            self._check()
            await self._wait(self._commit_changed.wait(), remaining=deadline - time.perf_counter())
            self._commit_changed.clear()
        self._check()
        self._uncommitted_bytes = 0

    async def send_audio(self, pcm):
        try:
            self.provider.input_format.validate_chunk(pcm, max_duration_ms=200)
            async with self._send_lock:
                self._check()
                if self._finished or (self.metrics['audio_bytes'] + len(pcm) > self.provider.max_audio_bytes
                        or self._elapsed() >= self.provider.max_record_seconds * 1000):
                    raise _error('invalid_request')
                # Bound the provider segment by bytes, independent of capture pace or speech content.
                offset = 0
                while offset < len(pcm):
                    count = min(len(pcm) - offset, _COMMIT_BYTES - self._uncommitted_bytes)
                    await self._send(pcm[offset:offset + count])
                    offset += count
                    self._uncommitted_bytes += count
                    self.metrics['audio_bytes'] += count
                    self.metrics['audio_ms'] = self.metrics['audio_bytes'] / 32
                    if self._uncommitted_bytes == _COMMIT_BYTES:
                        await self._commit()
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except Exception as exc:
            error = _error('invalid_request', exception=exc) if isinstance(exc, ValueError) else _transport_error(exc)
            self._fail(error)
            await self.aclose()
            raise error from None

    async def finish(self):
        try:
            async with self._send_lock:
                self._check()
                if self._finished:
                    return
                self._finished = True
                if not self.metrics['audio_bytes']:
                    raise _error('invalid_request')
                self.metrics['stop_sent_ms'] = self._elapsed()
                self._finalizing = True
                if self._uncommitted_bytes:
                    await self._commit()
                self._final = self._committed
                self.metrics['final_ms'] = self._elapsed()
                self.metrics['stop_to_final_ms'] = self.metrics['final_ms'] - self.metrics['stop_sent_ms']
                self._changed.set()
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except Exception as exc:
            self._fail(_transport_error(exc))
            await self.aclose()
            raise self._error from None

    def _fail(self, error):
        if self._error is None:
            self._error = error
        self._changed.set()
        self._commit_changed.set()

    async def _receive(self):
        try:
            while not self._closed:
                event = _decode(await self._wait(self._ws.recv(), remaining=self._deadline - time.perf_counter()))
                kind = event['message_type']
                if kind == 'warning':
                    self.metrics['provider_warning_count'] = self.metrics.get('provider_warning_count', 0) + 1
                    continue
                if kind not in {'partial_transcript', 'committed_transcript'}:
                    raise _error('invalid_response')
                text = event.get('text')
                if not isinstance(text, str) or len(self._committed) + len(text) > _MAX_TEXT:
                    raise _error('invalid_response')
                text = redact(text, secrets=self.provider.secrets)
                if kind == 'committed_transcript':
                    # Commits have no correlation ID. Our 20s bound prevents the provider's
                    # 36s automatic commit; an unsolicited segment violates that protocol.
                    if not self._pending_commit:
                        raise _error('invalid_response')
                    self._pending_commit = False
                    self._committed += text
                    self._partial = ''
                    self._commit_count += 1
                    self.metrics['committed_segments'] = self._commit_count
                    self._commit_changed.set()
                else:
                    self._partial = text
                    if text and self.metrics['first_partial_ms'] is None:
                        self.metrics['first_partial_ms'] = self._elapsed()
                # One coalescing slot bounds interim backpressure; stable segments are never dropped.
                self._partial_version += 1
                self._changed.set()
                if kind == 'committed_transcript' and self._finalizing:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(_transport_error(exc))
            await self.aclose()

    async def results(self):
        if self._reading:
            raise _error('invalid_request')
        self._reading = True
        seen = 0
        try:
            while True:
                if self._error is not None:
                    raise self._error
                if self._closed:
                    return
                if self._final is not None:
                    yield {'type': 'final', 'text': self._final, 'metrics': dict(self.metrics)}
                    return
                if seen != self._partial_version:
                    seen = self._partial_version
                    yield {'type': 'partial', 'text': self._committed + self._partial,
                           'metrics': dict(self.metrics)}
                    continue
                self._changed.clear()
                await self._changed.wait()
        finally:
            await self.aclose()

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        self._closed_event.set()
        self._changed.set()
        self._commit_changed.set()
        current = asyncio.current_task()
        tasks = [task for task in (self._reader, self._connecting)
                 if task is not None and task is not current]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        socket = self._ws
        if socket is None and self._connecting is not None and self._connecting.done() and not self._connecting.cancelled():
            try:
                socket = self._connecting.result()
            except Exception:
                pass
        if socket is not None:
            try:
                await asyncio.wait_for(socket.close(), timeout=1)
            except (Exception, asyncio.CancelledError):
                pass
        self.provider._connections.discard(self)
