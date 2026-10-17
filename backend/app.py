from backend.local_environment import load_local_environment

load_local_environment()

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, FileResponse
from pydantic import BaseModel, Field
from backend.asr_endpoint import recognize_socket
from backend.handsfree_endpoint import listen_socket
from backend.vad import vad_info
from backend.catalog import TOOLS, SKILLS, HOT_TOOL_NAMES, COLD_TOOL_NAMES
from backend.db import Database
from backend.persona import PERSONAS
from backend.session import SessionActor
from backend.settings import allowed_origins
from backend.services.factory import create_services
from backend.services.contracts import ProviderError
from backend.prompt_config import validate_all

actors = {}

@asynccontextmanager
async def lifespan(app):
    validate_all()
    app.state.services = services = create_services()
    app.state.db, app.state.ark, app.state.tts = Database(), services.llm, services.tts
    app.state.asr, app.state.maps, app.state.web, app.state.manual = services.asr, services.maps, services.web, services.manual
    await app.state.manual.prepare()
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
    await services.aclose()

app = FastAPI(title='Super XD Talker–Thinker', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=sorted(allowed_origins()), allow_methods=['GET', 'POST', 'HEAD'], allow_headers=['Content-Type', 'Range'])

@app.get('/api/health')
def health():
    return {'ok': True, 'sessions': len(actors), 'model_prewarm': app.state.prewarm, 'retrieval': retrieval_status(), 'manual': app.state.manual.info(), 'tts': app.state.tts.info(), 'asr': app.state.asr.info(), 'services': app.state.services.info()}

def retrieval_status():
    return {'status': 'disabled', 'model': None, 'reason': '车控目录按skill直接加载；车书检索状态见manual字段。'}

@app.get('/api/config')
def config():
    models = app.state.services.llm.info()['roles']
    return {'personas': [p.model_dump() for p in PERSONAS.values()],
            'catalog': {'tools': [t.model_dump() for t in TOOLS.values()], 'skills': [s.model_dump() for s in SKILLS.values()], 'hot_tool_names': HOT_TOOL_NAMES},
            'model': {'talker': models['talker']['model'], 'thinker': models['thinker']['model'], 'vehicle_planner': models['planner']['model']},
            'vehicle_control': {'mode': 'progressive_pro', 'hot_count': len(HOT_TOOL_NAMES),
                                'delegated_count': len(COLD_TOOL_NAMES)},
            'maps': app.state.maps.info(), 'web_search': app.state.web.info(), 'manual': app.state.manual.info(),
            'services': app.state.services.info(), 'cockpit': app.state.services.cockpit.info(),
            'retrieval': retrieval_status(), 'tts': app.state.tts.info(), 'asr': app.state.asr.info(),
            'vad': vad_info()}

@app.post('/api/sessions')
async def create_session():
    session_id = uuid4().hex[:16]
    actors[session_id] = SessionActor(session_id, app.state.db, app.state.ark, app.state.tts,
                                    maps=app.state.maps, web=app.state.web, manual=app.state.manual,
                                    cockpit=app.state.services.cockpit)
    return {'session_id': session_id}


@app.get('/api/services')
def services_status():
    return app.state.services.info()


@app.get('/api/media/catalog')
async def media_catalog(kind: str | None = None, query: str | None = None):
    from backend.cockpit.media import CockpitError, media_item
    provider = app.state.services.cockpit.media
    info = provider.info()
    if kind is not None and kind not in {'music', 'video', 'radio'}:
        raise HTTPException(400, 'Unknown media kind')
    selected = provider.provider_info(kind or 'music')
    if not selected.get('configured'):
        return {**info, 'ok': False, 'items': [], 'error': {'code': 'not_configured', 'message': 'Selected content CP is not configured'}}
    if query is not None:
        if not query.strip() or len(query) > 300:
            raise HTTPException(400, 'Media query must contain 1–300 characters')
        try:
            result = await provider.search(kind=kind or 'music', query=query, max_results=5)
            return {**result, **info}
        except (CockpitError, ProviderError) as error:
            return {**info, 'ok': False, 'items': [], 'error': {'code': error.code, 'message': str(error)}}
    entries = getattr(provider, 'entries', {})
    # Only an explicitly configured local catalog exposes playback URLs here.
    items = [media_item(item, playable=True) for item in entries.values()
             if kind is None or item['kind'] == kind]
    return {**info, 'ok': True, 'items': items}


@app.api_route('/api/media/files/{content_id}', methods=['GET', 'HEAD'])
async def media_file(content_id: str):
    path = app.state.services.media_files.get(content_id)
    if path is None or not path.is_file():
        raise HTTPException(404, 'Media content was not found in the configured catalog')
    return FileResponse(path, filename=path.name, content_disposition_type='inline',
                        headers={'Cache-Control': 'private, max-age=3600'})


@app.get('/api/manual/pdf')
async def manual_pdf():
    try:
        path = await app.state.manual.document()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from None
    return FileResponse(path, media_type='application/pdf', filename=path.name,
                        content_disposition_type='inline', headers={'Cache-Control': 'no-store'})


@app.get('/api/manual/{manual_id}/pages/{page}.png')
async def manual_page_image(manual_id: str, page: int):
    try:
        path = await app.state.manual.page_image(manual_id, page)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from None
    return FileResponse(path, media_type='image/png', headers={'Cache-Control': 'private, max-age=3600'})

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
    if origin and origin not in allowed_origins():
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
    if origin and origin not in allowed_origins():
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


@app.websocket('/ws/{session_id}/listen')
async def handsfree_websocket(ws: WebSocket, session_id: str):
    await listen_socket(ws, actors.get(session_id), app.state.asr)


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
