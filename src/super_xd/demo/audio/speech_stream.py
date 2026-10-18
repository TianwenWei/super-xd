"""Volcengine V3 bidirectional TTS: incremental text in, raw PCM frames out.

Implements the documented binary event protocol. This module neither stores
audio in trace JSON nor buffers a complete utterance before yielding audio.
"""
from __future__ import annotations

import asyncio
import gzip
import inspect
import json
import struct
import time
from typing import Any, AsyncIterator, Callable
from uuid import uuid4

STREAM_TTS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
SAMPLE_RATE = 24000
MAX_FRAME_BYTES = 10 * 1024 * 1024
EVENT_NAMES = {1: 'connection.start', 2: 'connection.finish', 50: 'connection.started',
    51: 'connection.failed', 52: 'connection.finished', 100: 'session.start',
    101: 'session.cancel', 102: 'session.finish', 150: 'session.started',
    151: 'session.cancelled', 152: 'session.finished', 153: 'session.failed',
    200: 'text.sent', 350: 'sentence.started', 351: 'sentence.finished', 352: 'audio'}


def encode_request(event: int, payload: dict | None = None, session_id: str | None = None) -> bytes:
    body = json.dumps(payload if payload is not None else {}, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')
    frame = bytearray(b'\x11\x14\x10\x00' + struct.pack('>I', event))
    if session_id is not None:
        encoded_id = session_id.encode('utf-8')
        frame.extend(struct.pack('>I', len(encoded_id)) + encoded_id)
    frame.extend(struct.pack('>I', len(body)) + body)
    return bytes(frame)


def decode_frame(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or len(raw) < 8 or len(raw) > MAX_FRAME_BYTES:
        raise RuntimeError('火山 Speech WS 返回了无效或过大的二进制帧。')
    version, header_size = raw[0] >> 4, (raw[0] & 15) * 4
    if version != 1 or header_size < 4 or header_size > len(raw):
        raise RuntimeError('火山 Speech WS 协议头无效。')
    message_type, flags = raw[1] >> 4, raw[1] & 15
    serialization, compression = raw[2] >> 4, raw[2] & 15
    offset = header_size

    def uint() -> int:
        nonlocal offset
        if offset + 4 > len(raw):
            raise RuntimeError('火山 Speech WS 返回了不完整的帧。')
        value = struct.unpack_from('>I', raw, offset)[0]
        offset += 4
        return value

    def blob() -> bytes:
        nonlocal offset
        size = uint()
        if offset + size > len(raw):
            raise RuntimeError('火山 Speech WS 返回了不完整的负载。')
        value = raw[offset:offset + size]
        offset += size
        return value

    error_code, event, identifier = None, None, None
    if message_type == 15:
        error_code = uint()
    else:
        if flags & 1:
            uint()  # Optional sequence number precedes the event.
        if flags & 4:
            event = uint()
            if event not in (1, 2):
                try:
                    identifier = blob().decode('utf-8')
                except UnicodeError:
                    raise RuntimeError('火山 Speech WS 会话标识无效。') from None
    body = blob()
    if offset != len(raw):
        raise RuntimeError('火山 Speech WS 帧包含意外的尾部数据。')
    if compression == 1:
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError):
            raise RuntimeError('火山 Speech WS 压缩负载无效。') from None
        if len(body) > MAX_FRAME_BYTES:
            raise RuntimeError('火山 Speech WS 解压后的负载过大。')
    elif compression != 0:
        raise RuntimeError('火山 Speech WS 使用了不支持的压缩格式。')
    data = None
    if serialization == 1:
        try:
            data = json.loads(body.decode('utf-8')) if body else {}
        except (UnicodeError, ValueError):
            raise RuntimeError('火山 Speech WS JSON 负载无效。') from None
    elif serialization != 0:
        raise RuntimeError('火山 Speech WS 使用了不支持的序列化格式。')
    return {'message_type': message_type, 'event': event, 'identifier': identifier,
            'error_code': error_code, 'payload': body, 'data': data}


class SpeechStream:
    def __init__(self, headers: dict[str, str], speaker: str, *, connect=None,
                 timeout_seconds: float = 45, max_text_chars: int = 4000,
                 max_audio_bytes: int = 20 * 1024 * 1024,
                 safe_message: Callable[[Any], str] = str,
                 connection: dict | None = None, recycle=None,
                 adapter_started: float | None = None, lease_wait_ms: float = 0,
                 prewarm_wait_ms: float = 0):
        self.headers, self.speaker = headers, speaker
        self.connect, self.timeout_seconds = connect, timeout_seconds
        self.max_text_chars, self.max_audio_bytes = max_text_chars, max_audio_bytes
        self.safe_message = safe_message
        self.websocket = None
        self.sender = None
        self.receiver = None
        self.source = None
        self.finished = False
        self.session_started = False
        self.session_id = str(uuid4())
        self._closing = False
        self.connection = connection
        self.recycle = recycle
        self.adapter_started = adapter_started
        self.lease_wait_ms, self.prewarm_wait_ms = lease_wait_ms, prewarm_wait_ms

    async def open_connection(self) -> dict:
        started = time.perf_counter()
        connect = self.connect
        if connect is None:
            from websockets.asyncio.client import connect
        self.websocket = await connect(STREAM_TTS_URL, additional_headers=self.headers,
            open_timeout=min(10, self.timeout_seconds), close_timeout=0.25,
            max_size=MAX_FRAME_BYTES, max_queue=4, compression=None)
        connected = (time.perf_counter() - started) * 1000
        try:
            await self.websocket.send(encode_request(1))
            frame = await self._receive()
            if frame['event'] != 50:
                raise RuntimeError('火山 Speech WS 未确认连接建立。')
        except BaseException:
            await self.websocket.close()
            self.websocket = None
            raise
        self.connection = {'websocket': self.websocket, 'connect_ms': connected,
                           'connection_ready_ms': (time.perf_counter() - started) * 1000,
                           'data': frame['data']}
        return self.connection

    async def _receive(self) -> dict[str, Any]:
        raw = await asyncio.wait_for(self.websocket.recv(), self.timeout_seconds)
        if isinstance(raw, str):
            raise RuntimeError('火山 Speech WS 返回文本错误：' + self.safe_message(raw))
        frame = decode_frame(raw)
        event = frame['event']
        if event is not None and event >= 100 and frame['identifier'] != self.session_id:
            raise RuntimeError('火山 Speech WS 返回了不属于当前会话的数据。')
        if frame['message_type'] == 15 or event in (51, 153):
            data = frame['data'] or {}
            code = frame['error_code'] if frame['error_code'] is not None else data.get('status_code')
            raise RuntimeError(f'火山 Speech WS 合成失败（code {code}）：' +
                               self.safe_message(data.get('message', '未知错误')))
        return frame

    async def events(self, texts: AsyncIterator[str], on_event=None) -> AsyncIterator[dict[str, Any]]:
        self.source = texts.__aiter__()
        started = time.perf_counter()
        metrics: dict[str, Any] = {'first_packet_ms': None, 'first_text_ms': None,
            'first_text_to_audio_ms': None, 'first_nonzero_packet_ms': None,
            'adapter_first_packet_ms': None, 'adapter_first_nonzero_packet_ms': None,
            'lease_wait_ms': self.lease_wait_ms, 'prewarm_wait_ms': self.prewarm_wait_ms,
            'audio_bytes': 0, 'audio_chunks': 0, 'text_chars': 0, 'reused': bool(self.connection)}

        def elapsed():
            return (time.perf_counter() - started) * 1000

        async def notify(event: dict):
            if on_event:
                result = on_event(event)
                if inspect.isawaitable(result):
                    await result

        def event_item(event: int, data=None):
            session_elapsed = elapsed()
            return {'type': 'event', 'event': EVENT_NAMES.get(event, f'provider.{event}'),
                    'event_code': event, 'session_id': self.session_id,
                    'metrics': {**metrics, 'elapsed_ms': session_elapsed,
                                'provider_session_elapsed_ms': session_elapsed,
                                'adapter_elapsed_ms': (time.perf_counter() - (self.adapter_started or started)) * 1000},
                    'data': data}

        base = {'namespace': 'BidirectionalTTS', 'req_params': {'speaker': self.speaker,
            'audio_params': {'format': 'pcm', 'sample_rate': SAMPLE_RATE},
            'additions': json.dumps({'disable_markdown_filter': True, 'disable_emoji_filter': True})}}
        try:
            if self.connection:
                self.websocket = self.connection['websocket']
                metrics['connect_ms'] = 0
                metrics['prewarm_connect_ms'] = self.connection['connect_ms']
                metrics['prewarm_connection_ready_ms'] = self.connection['connection_ready_ms']
            else:
                await self.open_connection()
                metrics['connect_ms'] = self.connection['connect_ms']
            metrics['connection_ready_ms'] = elapsed()
            item = event_item(50, self.connection['data'])
            await notify(item)
            yield item
            # Keep the connection warm, but do not leave an empty synthesis
            # session idle while a planner or external lookup is still running.
            first_text = None
            async for candidate in self.source:
                if not isinstance(candidate, str):
                    raise RuntimeError('Speech 增量输入必须是文本字符串。')
                if candidate.strip():
                    first_text = candidate
                    break
            if first_text is None:
                raise RuntimeError('Speech 增量输入为空或只含空白，未使用替代朗读。')
            await self.websocket.send(encode_request(100, {**base, 'event': 100}, self.session_id))
            frame = await self._receive()
            if frame['event'] != 150:
                raise RuntimeError('火山 Speech WS 未确认合成会话建立。')
            self.session_started = True
            metrics['session_ready_ms'] = elapsed()
            item = event_item(150, frame['data'])
            await notify(item)
            yield item

            async def send_texts():
                nonblank = False
                async def pending_texts():
                    yield first_text
                    async for value in self.source:
                        yield value
                async for text in pending_texts():
                    if not isinstance(text, str):
                        raise RuntimeError('Speech 增量输入必须是文本字符串。')
                    if not text:
                        continue
                    metrics['text_chars'] += len(text)
                    if metrics['text_chars'] > self.max_text_chars:
                        raise RuntimeError(f'Speech 增量文本超过 {self.max_text_chars} 字上限。')
                    nonblank = nonblank or bool(text.strip())
                    if metrics['first_text_ms'] is None:
                        metrics['first_text_ms'] = elapsed()
                    payload = {**base, 'event': 200, 'req_params': {**base['req_params'], 'text': text}}
                    await self.websocket.send(encode_request(200, payload, self.session_id))
                if not nonblank:
                    raise RuntimeError('Speech 增量输入为空或只含空白，未使用替代朗读。')
                await self.websocket.send(encode_request(102, session_id=self.session_id))
                metrics['input_finished_ms'] = elapsed()

            self.sender = asyncio.create_task(send_texts())
            source_done = False
            carry = b''
            while True:
                self.receiver = asyncio.create_task(self._receive())
                if not source_done:
                    done, _ = await asyncio.wait((self.sender, self.receiver), return_when=asyncio.FIRST_COMPLETED)
                    if self.sender in done:
                        await self.sender  # Surface source/send errors immediately.
                        source_done = True
                frame = await self.receiver
                self.receiver = None
                event = frame['event']
                if frame['message_type'] == 11:
                    if event not in (None, 352) or frame['data'] is not None:
                        raise RuntimeError('火山 Speech WS 音频帧格式无效。')
                    audio = carry + frame['payload']
                    carry = audio[-1:] if len(audio) % 2 else b''
                    audio = audio[:len(audio) - len(carry)]
                    if not audio:
                        continue
                    if metrics['first_packet_ms'] is None:
                        metrics['first_packet_ms'] = elapsed()
                        metrics['adapter_first_packet_ms'] = (time.perf_counter() - (self.adapter_started or started)) * 1000
                        metrics['first_text_to_audio_ms'] = metrics['first_packet_ms'] - metrics['first_text_ms']
                    nonzero = any(audio)
                    if nonzero and metrics['first_nonzero_packet_ms'] is None:
                        metrics['first_nonzero_packet_ms'] = elapsed()
                        metrics['adapter_first_nonzero_packet_ms'] = (time.perf_counter() - (self.adapter_started or started)) * 1000
                    metrics['audio_bytes'] += len(audio)
                    metrics['audio_chunks'] += 1
                    if metrics['audio_bytes'] > self.max_audio_bytes:
                        raise RuntimeError('火山 Speech WS 音频超过大小上限，已停止。')
                    # No JSON/base64 conversion, whole-utterance buffer, or end
                    # of input wait occurs on this audio path.
                    yield {'type': 'audio', 'audio': audio, 'sample_rate': SAMPLE_RATE,
                           'channels': 1, 'format': 'pcm_s16le', 'session_id': self.session_id,
                           'first_packet_ms': metrics['first_packet_ms'],
                           'first_text_to_audio_ms': metrics['first_text_to_audio_ms'],
                           'nonzero': nonzero, 'first_nonzero_packet_ms': metrics['first_nonzero_packet_ms'],
                           'adapter_first_packet_ms': metrics['adapter_first_packet_ms'],
                           'adapter_first_nonzero_packet_ms': metrics['adapter_first_nonzero_packet_ms'],
                           'lease_wait_ms': self.lease_wait_ms, 'prewarm_wait_ms': self.prewarm_wait_ms}
                    continue
                if event == 152:
                    if not self.sender.done():
                        raise RuntimeError('火山 Speech WS 在文本输入结束前提前结束会话。')
                    await self.sender
                    if carry:
                        raise RuntimeError('火山 Speech WS PCM 音频结束于不完整采样点。')
                    if not metrics['audio_bytes']:
                        raise RuntimeError('火山 Speech WS 未返回有效音频，未使用替代朗读。')
                    self.finished = True
                    metrics['total_ms'] = elapsed()
                elif event == 151:
                    raise RuntimeError('火山 Speech WS 合成会话已取消。')
                item = event_item(event, frame['data'])
                await notify(item)
                yield item
                if self.finished:
                    return
        except asyncio.TimeoutError:
            error = RuntimeError('火山 Speech WS 等待流式音频超时。')
            error.provider_code = 'timeout'
            raise error from None
        except asyncio.CancelledError:
            raise
        except RuntimeError:
            raise
        except Exception as exc:
            # Never expose WebSocket request/response headers in diagnostics.
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            detail = f'HTTP {status}' if status is not None else type(exc).__name__
            error = RuntimeError('火山 Speech WS 连接或流式合成失败：' + self.safe_message(detail))
            error.status_code = status
            raise error from None
        finally:
            await self.aclose()

    async def aclose(self):
        if self._closing:
            return
        self._closing = True
        for task in (self.sender, self.receiver):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (self.sender, self.receiver) if task), return_exceptions=True)
        if self.websocket:
            recycled = False
            if self.finished and self.recycle:
                try:
                    recycled = await self.recycle(self.connection)
                except Exception:
                    pass
            if recycled:
                self.websocket = None
        if self.websocket:
            try:
                if self.session_started and not self.finished:
                    await asyncio.wait_for(self.websocket.send(encode_request(101, session_id=self.session_id)), 0.25)
                await asyncio.wait_for(self.websocket.send(encode_request(2)), 0.25)
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.websocket.close(), 0.5)
            except Exception:
                pass
        close = getattr(self.source, 'aclose', None)
        if close:
            await close()
