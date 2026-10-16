"""One explicit microphone capture per socket; recognition never executes a query."""
import asyncio
import json
import time

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from backend.textutil import is_identifier
from backend.services.audio import PCM16K_ASR
from backend.services.contracts import ProviderError, redact
from backend.settings import allowed_origins

MAX_PCM_BYTES = int(PCM16K_ASR.bytes_per_ms * 60000)
MAX_FRAME_BYTES = int(PCM16K_ASR.bytes_per_ms * 200)
CAPTURE_TIMEOUT_SECONDS = 90
ALLOWED_ORIGINS = allowed_origins()
_active = {}


class CaptureCancelled(Exception):
    pass


class ASRInputError(ValueError):
    pass


def control_message(event):
    text = event.get('text')
    if not isinstance(text, str) or len(text) > 4096:
        raise ASRInputError('无效语音控制消息。')
    try:
        body = json.loads(text)
    except (ValueError, TypeError):
        raise ASRInputError('语音控制消息不是有效JSON。') from None
    if not isinstance(body, dict):
        raise ASRInputError('语音控制消息必须是对象。')
    return body


async def recognize_socket(ws: WebSocket, actor, asr):
    if actor is None:
        await ws.accept()
        await ws.close(code=4404)
        return
    if ws.headers.get('origin') and ws.headers['origin'] not in ALLOWED_ORIGINS:
        await ws.close(code=4403)
        return
    await ws.accept()
    audio_format = getattr(asr, 'input_format', PCM16K_ASR)
    capture_id, connection = None, None
    claimed, completed, ready = False, False, False
    started = time.perf_counter()
    marker = object()
    jobs = []
    audio = asyncio.Queue(maxsize=32)
    stopped = asyncio.Event()
    pcm_bytes = 0

    async def receive_audio():
        nonlocal pcm_bytes
        while True:
            event = await ws.receive()
            if event['type'] == 'websocket.disconnect':
                raise WebSocketDisconnect(event.get('code', 1000))
            chunk = event.get('bytes')
            if chunk is not None:
                if not ready or stopped.is_set():
                    raise ASRInputError('请在语音连接就绪后发送音频，结束后不能继续录音。')
                if not chunk or len(chunk) % 2 or len(chunk) > MAX_FRAME_BYTES:
                    raise ASRInputError('音频帧需为非空16位PCM，单帧最多200ms。')
                pcm_bytes += len(chunk)
                if pcm_bytes > MAX_PCM_BYTES:
                    raise ASRInputError('单次语音最多60秒，请分次说话。')
                item = chunk
            else:
                body = control_message(event)
                if body.get('capture_id') != capture_id:
                    raise ASRInputError('语音录音标识不匹配。')
                if body.get('type') == 'asr.cancel':
                    raise CaptureCancelled()
                if body.get('type') != 'asr.stop':
                    raise ASRInputError('未知语音控制消息。')
                if stopped.is_set():
                    continue
                stopped.set()
                item = None
            try:
                audio.put_nowait(item)
            except asyncio.QueueFull:
                raise ASRInputError('音频发送积压，请取消后重试。') from None

    async def send_audio():
        while True:
            chunk = await audio.get()
            if chunk is None:
                await connection.finish()
                return
            await connection.send_audio(chunk)

    async def provider_flow():
        nonlocal connection, completed, ready
        connection = await asr.open(capture_id)
        ready = True
        await ws.send_json({'type': 'asr.ready', 'capture_id': capture_id,
                            **audio_format.to_dict(),
                            'metrics': dict(getattr(connection, 'metrics', {}))})
        sender = asyncio.create_task(send_audio())
        jobs.append(sender)

        async def forward_results():
            nonlocal completed
            async for item in connection.results():
                kind = item.get('type')
                if kind not in {'partial', 'final'}:
                    raise ASRInputError('识别服务返回了未知消息。')
                text = item.get('text', '')
                if not isinstance(text, str) or len(text) > 4000:
                    raise ASRInputError('识别结果无效或超过4000字，请分次说话。')
                if kind == 'final' and not stopped.is_set():
                    raise ASRInputError('识别服务在录音结束前已关闭，本次内容没有发送。')
                if kind == 'final':
                    await sender  # All queued PCM and the terminal request must have been sent successfully.
                metrics = {**getattr(connection, 'metrics', {}), **item.get('metrics', {}),
                           'capture_elapsed_ms': (time.perf_counter() - started) * 1000,
                           'pcm_bytes': pcm_bytes, 'audio_duration_ms': audio_format.duration_ms(pcm_bytes)}
                await ws.send_json({'type': f'asr.{kind}', 'capture_id': capture_id,
                                    'text': text, 'metrics': metrics})
                if kind == 'final':
                    completed = True
                    actor.trace('asr.completed', {'capture_id': capture_id, 'text': text, 'metrics': metrics},
                                call_id=capture_id, status='completed')
                    return
            raise ASRInputError('识别连接未返回最终结果，本次内容没有发送。')

        results = asyncio.create_task(forward_results())
        jobs.append(results)
        done, _ = await asyncio.wait({sender, results}, return_when=asyncio.FIRST_COMPLETED)
        if sender in done:
            await sender  # A send failure must interrupt the otherwise waiting receiver.
            await results
        else:
            await results

    try:
        event = await asyncio.wait_for(ws.receive(), 10)
        if event['type'] == 'websocket.disconnect':
            return
        body = control_message(event)
        capture_id = body.get('capture_id')
        if (body.get('type') != 'asr.start' or not isinstance(capture_id, str)
                or not is_identifier(capture_id, max_length=80)):
            capture_id = None
            raise ASRInputError('请先发送有效的语音开始消息。')
        if (body.get('format') != audio_format.format or type(body.get('sample_rate')) is not int
                or body['sample_rate'] != audio_format.sample_rate or type(body.get('channels')) is not int
                or body['channels'] != audio_format.channels):
            raise ASRInputError('语音识别仅接受16kHz单声道16位PCM。')
        if actor.id in _active:
            raise ASRInputError('这个会话正在识别另一段语音，请先结束或取消。')
        _active[actor.id] = marker
        claimed = True
        await actor.post({'type': 'asr.capture', 'capture_id': capture_id, 'active': True})
        actor.trace('asr.started', {'capture_id': capture_id, 'provider': asr.info().get('provider'),
                                  **audio_format.to_dict()}, call_id=capture_id, status='running')
        receiver = asyncio.create_task(receive_audio())
        provider = asyncio.create_task(provider_flow())
        jobs.extend([receiver, provider])
        async with asyncio.timeout(CAPTURE_TIMEOUT_SECONDS):
            done, _ = await asyncio.wait({receiver, provider}, return_when=asyncio.FIRST_COMPLETED)
            # A cancel/disconnect already observed wins over a simultaneously
            # completed provider result; the frontend also guards its capture ID.
            if receiver in done:
                await receiver
            await provider
    except (CaptureCancelled, WebSocketDisconnect):
        if capture_id and claimed:
            actor.trace('asr.cancelled', {'capture_id': capture_id}, call_id=capture_id, status='cancelled')
    except Exception as exc:
        message = asr.safe_message(exc)
        code = 'asr_timeout' if isinstance(exc, TimeoutError) else 'asr_error'
        if isinstance(exc, TimeoutError):
            message = '语音识别等待超时，本次内容没有发送，请重试。'
        if capture_id and claimed:
            actor.trace('asr.error', {'capture_id': capture_id, 'error': message,
                                    'provider_error': redact(exc.public()) if isinstance(exc, ProviderError) else None,
                                    'diagnostics': redact(exc.details) if isinstance(exc, ProviderError) else None},
                        call_id=capture_id, status='failed')
        if ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.send_json({'type': 'asr.error', 'capture_id': capture_id, 'code': code, 'message': message,
                                    'provider_code': exc.code if isinstance(exc, ProviderError) else None})
            except (RuntimeError, WebSocketDisconnect):
                pass
    finally:
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        if connection is not None:
            await connection.aclose()
        if claimed:
            if _active.get(actor.id) is marker:
                _active.pop(actor.id)
            # A successful final retains the foreground lease until the client
            # submits input or explicitly releases it. This keeps queued Thinker
            # speech from consuming the gap while the client restores TTS.
            if not completed:
                await actor.post({'type': 'asr.capture', 'capture_id': capture_id, 'active': False})
        if ws.client_state == WebSocketState.CONNECTED and ws.application_state == WebSocketState.CONNECTED:
            try:
                await ws.close()
            except (RuntimeError, WebSocketDisconnect):
                pass
