"""ASGI capture lifecycle tests; doubles are explicit and never count as live ASR."""
import asyncio
from copy import deepcopy
import json

import pytest

from backend.app import actors, app
from backend.db import Database
from backend.session import SessionActor
from tests.test_session import configure_task_fixtures, wait_for


class Ark:
    async def stream(self, *args, **options):
        await asyncio.Event().wait()
        yield  # No model response can accidentally be attributed to ASR.


class Connection:
    def __init__(self):
        self.events = asyncio.Queue()
        self.audio = []
        self.finished = False
        self.closed = False
        self.send_error = None
        self.metrics = {'connect_ms': 1.0}

    async def send_audio(self, chunk):
        if self.send_error:
            raise RuntimeError(self.send_error)
        self.audio.append(chunk)

    async def finish(self):
        self.finished = True

    async def results(self):
        while True:
            event = await self.events.get()
            if isinstance(event, Exception):
                raise event
            yield event
            if event['type'] == 'final':
                return

    async def aclose(self):
        self.closed = True


class ASR:
    def __init__(self):
        self.connection = Connection()
        self.opening = asyncio.Event()
        self.gate = asyncio.Event()
        self.gate.set()
        self.open_cancelled = False

    def info(self):
        return {'configured': True, 'provider': 'explicit-test-asr'}

    def safe_message(self, exc):
        return str(exc).replace('fixture-secret', '[redacted]')[:240]

    async def open(self, request_id):
        self.request_id = request_id
        self.opening.set()
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.open_cancelled = True
            raise
        return self.connection


@pytest.fixture
async def context(tmp_path, monkeypatch):
    actor = SessionActor('asr-route', Database(tmp_path / 'asr.sqlite3'), Ark())
    configure_task_fixtures(actor)
    actors[actor.id] = actor
    await actor.attach()
    asr = ASR()
    monkeypatch.setattr(app.state, 'asr', asr, raising=False)
    yield actor, asr
    actors.pop(actor.id, None)
    await actor.close()


async def connect(path='/ws/asr-route/asr', origin='http://127.0.0.1:5173'):
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    scope = {'type': 'websocket', 'asgi': {'version': '3.0'}, 'scheme': 'ws',
             'path': path, 'raw_path': path.encode(), 'root_path': '', 'query_string': b'',
             'headers': [(b'origin', origin.encode())], 'client': ('127.0.0.1', 1234),
             'server': ('127.0.0.1', 8000), 'subprotocols': []}
    await incoming.put({'type': 'websocket.connect'})
    return incoming, outgoing, asyncio.create_task(app(scope, incoming.get, outgoing.put))


async def receive(queue):
    result = await asyncio.wait_for(queue.get(), 1)
    return json.loads(result['text']) if result.get('text') else result


async def control(queue, kind, capture='capture-a', **values):
    await queue.put({'type': 'websocket.receive', 'text': json.dumps(
        {'type': kind, 'capture_id': capture, **values})})


async def start():
    incoming, outgoing, runner = await connect()
    assert (await receive(outgoing))['type'] == 'websocket.accept'
    await control(incoming, 'asr.start', format='pcm_s16le', sample_rate=16000, channels=1)
    assert (await receive(outgoing))['type'] == 'asr.ready'
    return incoming, outgoing, runner


async def test_partial_and_final_are_snapshots_and_do_not_execute_any_query(context):
    actor, asr = context
    vehicle = deepcopy(actor.vehicle)
    incoming, outgoing, runner = await start()
    try:
        await incoming.put({'type': 'websocket.receive', 'bytes': b'\x10\x00' * 3200})
        await asr.connection.events.put({'type': 'partial', 'text': '把空调'})
        partial = await receive(outgoing)
        assert partial['text'] == '把空调' and partial['capture_id'] == 'capture-a'
        assert actor.turns == [] and actor.vehicle == vehicle
        await control(incoming, 'asr.stop')
        await asr.connection.events.put({'type': 'final', 'text': '把空调调到23度。'})
        final = await receive(outgoing)
        assert final['type'] == 'asr.final' and final['text'] == '把空调调到23度。'
        assert final['metrics']['audio_duration_ms'] == 200
        await asyncio.wait_for(runner, 1)
        await actor.queue.join()
        assert asr.connection.finished and asr.connection.closed
        assert len(asr.connection.audio) == 1
        assert actor.turns == [] and actor.vehicle == vehicle
        assert actor.asr_capture_id == 'capture-a'
        await actor.post({'type': 'input', 'text': final['text']})
        await actor.queue.join()
        assert actor.current.text == final['text'] and actor.asr_capture_id is None
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.parametrize('cancel', [True, False])
async def test_cancel_or_disconnect_during_open_does_not_wait_for_provider(context, cancel):
    actor, asr = context
    asr.gate.clear()
    incoming, outgoing, runner = await connect()
    await receive(outgoing)
    await control(incoming, 'asr.start', format='pcm_s16le', sample_rate=16000, channels=1)
    await asyncio.wait_for(asr.opening.wait(), 1)
    if cancel:
        await control(incoming, 'asr.cancel')
    else:
        await incoming.put({'type': 'websocket.disconnect', 'code': 1000})
    await asyncio.wait_for(runner, 1)
    await actor.queue.join()
    assert asr.open_cancelled and actor.asr_capture_id is None and not actor.turns


async def test_cancel_after_partial_closes_provider_and_discards_late_final(context):
    actor, asr = context
    incoming, outgoing, runner = await start()
    await asr.connection.events.put({'type': 'partial', 'text': '开窗'})
    assert (await receive(outgoing))['type'] == 'asr.partial'
    await control(incoming, 'asr.cancel')
    await asyncio.wait_for(runner, 1)
    await asr.connection.events.put({'type': 'final', 'text': '开窗到一半'})
    await actor.queue.join()
    assert asr.connection.closed and actor.asr_capture_id is None and not actor.turns
    assert all('asr.final' not in str(event) for event in list(outgoing._queue))


async def test_provider_final_before_user_stop_is_an_error_not_an_action(context):
    actor, asr = context
    _, outgoing, runner = await start()
    await asr.connection.events.put({'type': 'final', 'text': '设置音量100'})
    assert (await receive(outgoing))['type'] == 'asr.error'
    await asyncio.wait_for(runner, 1)
    assert not actor.turns and asr.connection.closed


@pytest.mark.parametrize('chunk', [b'\x01', b'\x01\x00' * 3201, b''])
async def test_invalid_pcm_is_rejected_without_query_or_unbounded_buffer(context, chunk):
    actor, asr = context
    incoming, outgoing, runner = await start()
    await incoming.put({'type': 'websocket.receive', 'bytes': chunk})
    assert (await receive(outgoing))['type'] == 'asr.error'
    await asyncio.wait_for(runner, 1)
    assert not actor.turns and not asr.connection.audio and asr.connection.closed


async def test_send_failure_interrupts_result_wait_and_redacts_error(context):
    actor, asr = context
    asr.connection.send_error = 'bad request fixture-secret'
    incoming, outgoing, runner = await start()
    await incoming.put({'type': 'websocket.receive', 'bytes': b'\x01\x00'})
    error = await receive(outgoing)
    assert error['type'] == 'asr.error' and '[redacted]' in error['message']
    assert 'fixture-secret' not in str(error)
    await asyncio.wait_for(runner, 1)
    assert not actor.turns and asr.connection.closed


@pytest.mark.parametrize(('path', 'origin', 'code'), [
    ('/ws/missing/asr', 'http://127.0.0.1:5173', 4404),
    ('/ws/asr-route/asr', 'https://untrusted.example', 4403),
])
async def test_asr_route_preserves_missing_session_and_origin_guards(context, path, origin, code):
    _, outgoing, runner = await connect(path, origin)
    event = await receive(outgoing)
    if code == 4404:
        assert event['type'] == 'websocket.accept'
        event = await receive(outgoing)
    assert event['type'] == 'websocket.close' and event['code'] == code
    await asyncio.wait_for(runner, 1)


async def test_capture_holds_proactive_result_until_released_and_ignores_old_release(context):
    actor, _ = context
    control = await actor.attach()
    await actor.post({'type': 'asr.capture', 'capture_id': 'capture-new', 'active': True})
    await actor.queue.join()
    task = actor.db.create_task(actor.id, '测试后台结果')
    actor.db.save_task(task['id'], 1, status='completed', summary='已完成',
                       result={'summary': '官方核对已完成。', 'spoken_summary': '官方核对已完成。'})
    event = actor.db.events_after(0, actor.id)[-1]
    event['payload']['task'] = actor.db.get_task(task['id'])
    await actor.post({'type': '_task_event', 'event': event})
    await actor.post({'type': '_announce'})
    await actor.queue.join()
    assert not actor.turns and task['id'] in actor.announcements
    await actor.post({'type': 'asr.capture', 'capture_id': 'capture-old', 'active': False})
    await actor.post({'type': '_announce'})
    await actor.queue.join()
    assert not actor.turns and actor.asr_capture_id == 'capture-new'
    await actor.post({'type': 'asr.capture', 'capture_id': 'capture-new', 'active': False})
    await actor.post({'type': '_announce'})
    await wait_for(control, 'stream.done')
    assert actor.current.proactive and task['id'] not in actor.announcements
