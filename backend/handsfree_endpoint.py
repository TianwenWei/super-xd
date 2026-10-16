"""Continuous local VAD plus cloud ASR; a new segment supersedes any old final."""
import asyncio
from dataclasses import dataclass, field
import time
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect

from backend.asr_endpoint import ALLOWED_ORIGINS, ASRInputError, _active, control_message
from backend.textutil import is_identifier
from backend.vad import FRAME_BYTES, SileroVAD, SpeechGate, vad_info


@dataclass
class Segment:
    id: str = field(default_factory=lambda: uuid4().hex)
    audio: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=96))
    ended: bool = False
    completed: bool = False
    pcm_bytes: int = 0
    started: float = field(default_factory=time.perf_counter)
    job: asyncio.Task | None = None


async def listen_socket(ws: WebSocket, actor, asr, *, vad_factory=SileroVAD):
    if actor is None or (ws.headers.get('origin') and ws.headers['origin'] not in ALLOWED_ORIGINS):
        await ws.close(code=4404 if actor is None else 4403)
        return
    await ws.accept()
    listen_id, segment, claimed = None, None, False
    marker = object()
    jobs = set()
    send_lock = asyncio.Lock()

    async def emit(kind, **payload):
        async with send_lock:
            if (kind in {'voice.partial', 'voice.final'} and
                    (segment is None or payload.get('capture_id') != segment.id)):
                return
            await ws.send_json({'type': kind, 'listen_id': listen_id, **payload})

    async def recognize(target):
        connection, sender = None, None
        try:
            async with asyncio.timeout(75):
                connection = await asr.open(target.id)

                async def send_audio():
                    while True:
                        pcm = await target.audio.get()
                        if pcm is None:
                            await connection.finish()
                            return
                        await connection.send_audio(pcm)

                sender = asyncio.create_task(send_audio())

                async def results():
                    async for item in connection.results():
                        if segment is not target:
                            return
                        kind, text = item.get('type'), item.get('text', '')
                        if kind not in {'partial', 'final'} or not isinstance(text, str) or len(text) > 4000:
                            raise ASRInputError('语音识别结果无效。')
                        if kind == 'final':
                            if not target.ended:
                                raise ASRInputError('识别服务提前关闭，本段语音未发送。')
                            await sender
                            if segment is not target:
                                return
                            metrics = {**getattr(connection, 'metrics', {}), **item.get('metrics', {}),
                                       'pcm_bytes': target.pcm_bytes,
                                       'capture_elapsed_ms': (time.perf_counter() - target.started) * 1000}
                            actor.trace('asr.completed', {'capture_id': target.id, 'text': text,
                                        'metrics': metrics, 'mode': 'handsfree'}, call_id=target.id, status='completed')
                            await emit('voice.final', capture_id=target.id, text=text, metrics=metrics)
                            target.completed = True
                            return
                        await emit('voice.partial', capture_id=target.id, text=text)
                    raise ASRInputError('语音识别没有返回完整结果。')

                receiver = asyncio.create_task(results())
                try:
                    done, _ = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
                    if sender in done:
                        await sender
                    await receiver
                finally:
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)
        except asyncio.CancelledError:
            actor.trace('asr.cancelled', {'capture_id': target.id, 'mode': 'handsfree'},
                        call_id=target.id, status='cancelled')
            raise
        except Exception as exc:
            message = asr.safe_message(exc)
            actor.trace('asr.error', {'capture_id': target.id, 'error': message, 'mode': 'handsfree'},
                        call_id=target.id, status='failed')
            if segment is target:
                await emit('voice.error', capture_id=target.id, message=message)
        finally:
            if sender:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            if connection:
                await connection.aclose()
            if not target.completed:
                await actor.post({'type': 'asr.capture', 'capture_id': target.id, 'active': False})

    try:
        event = await asyncio.wait_for(ws.receive(), 10)
        body = control_message(event)
        listen_id = body.get('listen_id')
        if (body.get('type') != 'voice.start' or not is_identifier(listen_id, max_length=80)
                or body.get('format') != 'pcm_s16le' or type(body.get('sample_rate')) is not int
                or body['sample_rate'] != 16000 or type(body.get('channels')) is not int or body['channels'] != 1):
            raise ASRInputError('免按键对话需要16kHz单声道PCM和有效监听标识。')
        if not asr.info().get('configured'):
            raise ASRInputError('火山 ASR 尚未配置，无法开始免按键对话。')
        if actor.id in _active:
            raise ASRInputError('这个会话已有语音连接，请先关闭另一处录音。')
        _active[actor.id], claimed = marker, True
        vad = await asyncio.to_thread(vad_factory)
        gate = SpeechGate()
        await emit('voice.ready', vad=vad_info())
        while True:
            event = await asyncio.wait_for(ws.receive(), 15)
            if event['type'] == 'websocket.disconnect':
                break
            pcm = event.get('bytes')
            if pcm is None:
                body = control_message(event)
                if body.get('type') == 'voice.stop' and body.get('listen_id') == listen_id:
                    break
                raise ASRInputError('未知免按键语音消息。')
            if len(pcm) != FRAME_BYTES:
                raise ASRInputError('免按键语音每帧必须是32ms的16位PCM。')
            started = time.perf_counter()
            probability = await asyncio.to_thread(vad.score, pcm)
            inference_ms = (time.perf_counter() - started) * 1000
            result = gate.feed(pcm, probability)
            if result['event'] == 'start':
                if segment and segment.job and not segment.job.done():
                    segment.job.cancel()
                segment = Segment()
                await actor.post({'type': 'asr.capture', 'capture_id': segment.id,
                                  'active': True, 'interrupt': True})
                actor.trace('vad.speech_started', {'capture_id': segment.id, 'probability': probability,
                            'inference_with_dispatch_ms': inference_ms, 'preroll_ms': 320},
                            call_id=segment.id, status='running')
                await emit('voice.speech_start', capture_id=segment.id, vad_ms=inference_ms)
                segment.job = asyncio.create_task(recognize(segment))
                jobs.add(segment.job)
            if segment and not segment.ended:
                for chunk in result['audio']:
                    segment.pcm_bytes += len(chunk)
                    if segment.pcm_bytes > 16000 * 2 * 55:
                        raise ASRInputError('连续说话超过55秒，本段未发送，请停顿后分段说。')
                    try:
                        segment.audio.put_nowait(chunk)
                    except asyncio.QueueFull:
                        raise ASRInputError('语音识别上传积压，已停止监听，请重试。') from None
                if result['event'] == 'end':
                    segment.ended = True
                    segment.audio.put_nowait(None)
                    actor.trace('vad.speech_ended', {'capture_id': segment.id, 'silence_ms': 640,
                                'pcm_bytes': segment.pcm_bytes}, call_id=segment.id, status='completed')
                    await emit('voice.speech_end', capture_id=segment.id)
            # Retrieve finished task exceptions and bound a long-running listener's bookkeeping.
            for job in list(jobs):
                if job.done():
                    job.exception() if not job.cancelled() else None
                    jobs.discard(job)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await emit('voice.error', message=asr.safe_message(exc))
        except (RuntimeError, WebSocketDisconnect):
            pass
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        if segment:
            await actor.post({'type': 'asr.capture', 'capture_id': segment.id, 'active': False})
        if claimed and _active.get(actor.id) is marker:
            _active.pop(actor.id)
        try:
            await ws.close()
        except (RuntimeError, WebSocketDisconnect):
            pass
