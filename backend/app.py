import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field
from backend.tts import VolcanoTTS
from backend.asr import VolcanoASR
from backend.asr_endpoint import recognize_socket
from backend.catalog import TOOLS, SKILLS, HOT_TOOL_NAMES, COLD_TOOL_NAMES
from backend.db import Database
from backend.models import ArkClient
from backend.persona import PERSONAS
from backend.session import SessionActor
from backend.google_maps import GoogleMapsClient
from backend.settings import TALKER_MODEL, THINKER_MODEL, VEHICLE_CONTROL_MODEL

actors = {}

@asynccontextmanager
async def lifespan(app):
    app.state.db, app.state.ark, app.state.tts = Database(), ArkClient(), VolcanoTTS()
    app.state.asr = VolcanoASR()
    app.state.maps = GoogleMapsClient()
    app.state.prewarm = {'ok': None, 'status': 'warming'}
    async def warm_model():
        app.state.prewarm = await app.state.ark.prewarm()
    jobs = [asyncio.create_task(warm_model())]
    yield
    for job in jobs:
        job.cancel()
    await asyncio.gather(*jobs, return_exceptions=True)
    await asyncio.gather(*(actor.close() for actor in actors.values()))
    actors.clear()
    await app.state.ark.aclose()
    await app.state.tts.aclose()
    await app.state.asr.aclose()
    await app.state.maps.aclose()

app = FastAPI(title='Super XD Talker–Thinker', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=['http://localhost:5173', 'http://127.0.0.1:5173'], allow_methods=['GET', 'POST'], allow_headers=['Content-Type'])

@app.get('/api/health')
def health():
    return {'ok': True, 'sessions': len(actors), 'model_prewarm': app.state.prewarm, 'retrieval': retrieval_status(), 'tts': app.state.tts.info(), 'asr': app.state.asr.info()}

def retrieval_status():
    return {'status': 'disabled', 'model': None, 'reason': '本版不实现检索；按skill分类直接加载。'}

@app.get('/api/config')
def config():
    return {'personas': [p.model_dump() for p in PERSONAS.values()],
            'catalog': {'tools': [t.model_dump() for t in TOOLS.values()], 'skills': [s.model_dump() for s in SKILLS.values()], 'hot_tool_names': HOT_TOOL_NAMES},
            'model': {'talker': TALKER_MODEL, 'thinker': THINKER_MODEL, 'vehicle_planner': VEHICLE_CONTROL_MODEL},
            'vehicle_control': {'mode': 'progressive_pro', 'hot_count': len(HOT_TOOL_NAMES),
                                'delegated_count': len(COLD_TOOL_NAMES)},
            'maps': app.state.maps.info(),
            'retrieval': retrieval_status(), 'tts': app.state.tts.info(), 'asr': app.state.asr.info()}

@app.post('/api/sessions')
async def create_session():
    session_id = uuid4().hex[:16]
    actors[session_id] = SessionActor(session_id, app.state.db, app.state.ark, app.state.tts, maps=app.state.maps)
    return {'session_id': session_id}

@app.get('/api/sessions/{session_id}')
def get_session(session_id):
    actor = actors.get(session_id)
    if not actor:
        raise HTTPException(404, 'Session not found; create a new session')
    return actor.snapshot()


@app.post('/api/sessions/{session_id}/vehicle/commands/{command_id}/execute')
async def simulate_vehicle_command(session_id: str, command_id: str):
    actor = actors.get(session_id)
    if not actor:
        raise HTTPException(404, 'Session not found')
    try:
        return await actor.simulator.execute(command_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.get('/api/sessions/{session_id}/traces')
def get_traces(session_id: str, after_id: int = Query(0, ge=0),
               turn_id: str | None = None, task_id: str | None = None,
               limit: int = Query(200, ge=1, le=200)):
    actor = actors.get(session_id)
    if not actor:
        raise HTTPException(404, 'Session not found; create a new session')
    events = actor.db.traces_after(session_id, after_id, turn_id, task_id, limit + 1)
    return {'events': events[:limit], 'has_more': len(events) > limit,
            'next_after_id': events[min(len(events), limit) - 1]['trace_id'] if events else after_id,
            'recording_error': actor.trace_error}

@app.websocket('/ws/{session_id}')
async def websocket(ws: WebSocket, session_id: str):
    actor = actors.get(session_id)
    if not actor:
        await ws.accept()
        await ws.close(code=4404)
        return
    # This prototype is local-only. Reject cross-origin browser websocket requests.
    origin = ws.headers.get('origin')
    if origin and origin not in {'http://localhost:5173', 'http://127.0.0.1:5173', 'http://localhost:8000', 'http://127.0.0.1:8000'}:
        await ws.close(code=4403)
        return
    await ws.accept()
    outgoing = await actor.attach()
    async def sender():
        while True:
            await ws.send_json(await outgoing.get())
    send_task = asyncio.create_task(sender())
    try:
        while True:
            message = await ws.receive_json()
            if not isinstance(message, dict) or not isinstance(message.get('type'), str) or message['type'].startswith('_'):
                await ws.send_json({'type': 'error', 'message': '无效消息'})
                continue
            if message['type'] == 'vehicle.execute':
                await ws.send_json({'type': 'error', 'message': '车控需要模拟器实际回执，不能只发送ACK。'})
                continue
            await actor.post(message)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        send_task.cancel()
        await asyncio.gather(send_task, return_exceptions=True)
        await actor.post({'type': '_detach', 'outgoing': outgoing})


@app.websocket('/ws/{session_id}/audio')
async def audio_websocket(ws: WebSocket, session_id: str):
    actor = actors.get(session_id)
    if not actor:
        await ws.accept()
        await ws.close(code=4404)
        return
    origin = ws.headers.get('origin')
    if origin and origin not in {'http://localhost:5173', 'http://127.0.0.1:5173',
                                 'http://localhost:8000', 'http://127.0.0.1:8000'}:
        await ws.close(code=4403)
        return
    await ws.accept()
    outgoing = await actor.attach_audio()
    async def sender():
        while True:
            packet = await outgoing.get()
            if packet is None:
                await ws.close(code=4408)
                return
            if isinstance(packet, bytes):
                await ws.send_bytes(packet)
            else:
                await ws.send_json(packet)
    send_task = asyncio.create_task(sender())
    try:
        # This socket is server → client only. Receiving keeps disconnect
        # detection responsive even while a model/TTS request is pending.
        while True:
            event = await ws.receive()
            if event['type'] == 'websocket.disconnect': break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        send_task.cancel()
        await asyncio.gather(send_task, return_exceptions=True)
        await actor.post({'type': '_audio_detach', 'outgoing': outgoing})


@app.websocket('/ws/{session_id}/asr')
async def asr_websocket(ws: WebSocket, session_id: str):
    await recognize_socket(ws, actors.get(session_id), app.state.asr)


class TTSRequest(BaseModel):
    turn_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=4000)


@app.post('/api/sessions/{session_id}/tts')
async def tts_audio(session_id: str, body: TTSRequest):
    actor = actors.get(session_id)
    if not actor:
        raise HTTPException(404, '会话不存在，请新建会话')
    turn = actor.current
    if not turn or turn.id != body.turn_id or turn.audio_cancelled.is_set():
        raise HTTPException(409, '轮次已过期，已丢弃音频请求')
    if not turn.finished or body.text != turn.output:
        raise HTTPException(400, '只能朗读当前已完成轮次的文本')
    synthesis = asyncio.create_task(app.state.tts.synthesize_with_metrics(body.text))
    cancelled = asyncio.create_task(turn.audio_cancelled.wait())
    try:
        await asyncio.wait({synthesis, cancelled}, return_when=asyncio.FIRST_COMPLETED)
        if cancelled.done() or actor.current is not turn:
            raise HTTPException(409, '新轮次已开始，旧音频合成已取消')
        audio = await synthesis
        return Response(content=audio['audio'], media_type='audio/mpeg', headers={
            'Cache-Control': 'no-store', 'X-Turn-Id': turn.id,
            'X-TTS-First-Packet-Ms': str(round(audio['first_packet_ms'], 1)),
            'X-TTS-Synthesis-Ms': str(round(audio['total_ms'], 1)),
        })
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from None
    finally:
        for job in (synthesis, cancelled):
            if not job.done(): job.cancel()
        await asyncio.gather(synthesis, cancelled, return_exceptions=True)
