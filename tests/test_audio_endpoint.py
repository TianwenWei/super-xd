"""Real ASGI websocket routing with explicitly injected local audio/model doubles."""
import asyncio
import json

import pytest

from backend.app import actors, app
from backend.db import Database
from backend.session import SessionActor


class Ark:
    def __init__(self):
        self.gate = asyncio.Event()

    async def stream(self, messages, tools, **options):
        await self.gate.wait()
        yield {'type': 'delta', 'text': '路由测试回答。'}
        yield {'type': 'message', 'message': {'role': 'assistant', 'content': '路由测试回答。'}}


class TTS:
    def __init__(self):
        self.started, self.cancelled = asyncio.Event(), asyncio.Event()

    async def stream_synthesize(self, texts):
        self.started.set()
        try:
            async for text in texts:
                yield {'type': 'audio', 'audio': b'\x01\x00\x03\x00'}
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


@pytest.fixture
async def endpoint_actor(tmp_path):
    ark, tts = Ark(), TTS()
    actor = SessionActor('ws-audio-route', Database(tmp_path / 'audio.sqlite3'), ark, tts)
    actors[actor.id] = actor
    control = await actor.attach()
    await control.get()
    yield actor, control, ark, tts
    actors.pop(actor.id, None)
    await actor.close()


async def connect(path, origin='http://127.0.0.1:5173'):
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    scope = {'type': 'websocket', 'asgi': {'version': '3.0'}, 'scheme': 'ws',
             'path': path, 'raw_path': path.encode(), 'root_path': '', 'query_string': b'',
             'headers': [(b'origin', origin.encode())], 'client': ('127.0.0.1', 1234),
             'server': ('127.0.0.1', 8000), 'subprotocols': []}
    await incoming.put({'type': 'websocket.connect'})
    runner = asyncio.create_task(app(scope, incoming.get, outgoing.put))
    return incoming, outgoing, runner


async def receive(queue):
    return await asyncio.wait_for(queue.get(), 1)


async def test_audio_route_orders_started_binary_and_end_and_preserves_identity(endpoint_actor):
    actor, control, ark, _ = endpoint_actor
    incoming, outgoing, runner = await connect(f'/ws/{actor.id}/audio')
    try:
        assert (await receive(outgoing))['type'] == 'websocket.accept'
        await actor.post({'type': 'audio.configure', 'enabled': True})
        await actor.post({'type': 'input', 'text': '说一句话', 'client_request_id': 'route-request'})
        started_wire = await receive(outgoing)
        started = json.loads(started_wire['text'])
        assert started['type'] == 'audio.started' and started['client_request_id'] == 'route-request'
        ark.gate.set()
        binary = await receive(outgoing)
        assert binary['bytes'][:32].decode() == started['playback_id']
        assert binary['bytes'][32:] == b'\x01\x00\x03\x00'
        metrics = json.loads((await receive(outgoing))['text'])
        ended = json.loads((await receive(outgoing))['text'])
        assert metrics['type'] == 'audio.metrics' and metrics['turn_id'] == started['turn_id']
        assert ended == {'type': 'audio.ended', 'turn_id': started['turn_id'], 'playback_id': started['playback_id']}
        assert actor.speaking
    finally:
        await incoming.put({'type': 'websocket.disconnect', 'code': 1000})
        await asyncio.wait_for(runner, 1)
        await actor.queue.join()


async def test_audio_socket_disconnect_cancels_provider_but_preserves_model_and_task(endpoint_actor):
    actor, control, ark, tts = endpoint_actor
    task = actor.db.create_task(actor.id, '明确注入的测试任务')
    incoming, outgoing, runner = await connect(f'/ws/{actor.id}/audio')
    assert (await receive(outgoing))['type'] == 'websocket.accept'
    await actor.post({'type': 'audio.configure', 'enabled': True})
    await actor.post({'type': 'input', 'text': '继续聊天'})
    await asyncio.wait_for(tts.started.wait(), 1)
    turn = actor.current
    await incoming.put({'type': 'websocket.disconnect', 'code': 1000})
    await asyncio.wait_for(runner, 1)
    await actor.queue.join()
    await asyncio.wait_for(tts.cancelled.wait(), 1)
    assert turn.audio_cancelled.is_set() and not actor.speaking
    assert not turn.model_task.done() and actor.db.get_task(task['id'])['status'] == 'queued'
    ark.gate.set()
    async with asyncio.timeout(1):
        while (await control.get())['type'] != 'stream.done':
            pass
    assert turn.output == '路由测试回答。'


@pytest.mark.parametrize(('path', 'origin', 'code'), [
    ('/ws/no-such-session/audio', 'http://127.0.0.1:5173', 4404),
    ('/ws/ws-audio-route/audio', 'https://untrusted.example', 4403),
])
async def test_audio_route_rejects_missing_session_and_cross_origin(endpoint_actor, path, origin, code):
    _, outgoing, runner = await connect(path, origin)
    event = await receive(outgoing)
    if code == 4404:
        assert event['type'] == 'websocket.accept'  # Browser can observe the explicit expired-session code.
        event = await receive(outgoing)
    assert event == {'type': 'websocket.close', 'code': code, 'reason': ''}
    await asyncio.wait_for(runner, 1)
