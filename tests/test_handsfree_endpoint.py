"""Protocol/actor regression tests with explicit acoustic and ASR doubles."""
import asyncio
from copy import deepcopy
import json

import pytest

from backend.db import Database
from backend.handsfree_endpoint import listen_socket
from backend.session import SessionActor


class Ark:
    async def stream(self, *args):
        await asyncio.Event().wait()
        yield


class VAD:
    def score(self, pcm):
        return .95 if pcm[0] else .01


class Connection:
    def __init__(self):
        self.events = asyncio.Queue()
        self.audio = []
        self.finished = asyncio.Event()
        self.closed = False
        self.preroll_sent = asyncio.Event()

    async def send_audio(self, pcm):
        self.audio.append(pcm)
        if len(self.audio) >= 10:
            self.preroll_sent.set()

    async def finish(self):
        self.finished.set()

    async def results(self):
        while True:
            event = await self.events.get()
            yield event
            if event['type'] == 'final':
                return

    async def aclose(self):
        self.closed = True


class ASR:
    def __init__(self):
        self.connections = []
        self.opened = asyncio.Queue()

    def info(self):
        return {'configured': True}

    def safe_message(self, error):
        return str(error)

    async def open(self, capture):
        connection = Connection()
        self.connections.append(connection)
        self.opened.put_nowait(connection)
        return connection


class Socket:
    headers = {'origin': 'http://localhost:5173'}

    def __init__(self):
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()

    async def accept(self):
        pass

    async def close(self, **kwargs):
        pass

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, message):
        await self.outgoing.put(message)

    async def event(self, kind):
        async with asyncio.timeout(2):
            while True:
                message = await self.outgoing.get()
                if message['type'] == kind:
                    return message

    async def frames(self, voiced, count):
        for _ in range(count):
            await self.incoming.put({'type': 'websocket.receive', 'bytes': bytes([int(voiced), 0]) * 512})


@pytest.fixture
async def voice(tmp_path):
    actor = SessionActor('handsfree-test', Database(tmp_path / 'voice.sqlite3'), Ark())
    await actor.attach()
    socket, asr = Socket(), ASR()
    runner = asyncio.create_task(listen_socket(socket, actor, asr, vad_factory=VAD))
    await socket.incoming.put({'type': 'websocket.receive', 'text': json.dumps({
        'type': 'voice.start', 'listen_id': 'listener-a', 'sample_rate': 16000,
        'format': 'pcm_s16le', 'channels': 1})})
    await socket.event('voice.ready')
    yield actor, socket, asr
    await socket.incoming.put({'type': 'websocket.disconnect'})
    await asyncio.wait_for(runner, 2)
    await actor.close()


async def start_speech(socket, asr):
    await socket.frames(False, 10)
    await socket.frames(True, 3)
    capture = (await socket.event('voice.speech_start'))['capture_id']
    connection = await asyncio.wait_for(asr.opened.get(), 1)
    await asyncio.wait_for(connection.preroll_sent.wait(), 1)
    return capture


async def test_silence_has_no_asr_and_speech_only_stops_playback_before_final(voice):
    actor, socket, asr = voice
    await actor.post({'type': 'input', 'text': '讲个故事'})
    await actor.queue.join()
    old = actor.current
    vehicle = deepcopy(actor.vehicle)
    capture = await start_speech(socket, asr)
    await actor.queue.join()
    # Noise must not void the turn's work; only a final transcript starts a new turn.
    assert not old.interrupted and old.audio_cancelled.is_set()
    assert actor.asr_capture_id == capture
    assert len(actor.turns) == 1 and actor.vehicle == vehicle
    assert len(asr.connections) == 1
    assert len(asr.connections[0].audio) == 10


async def test_final_is_only_a_candidate_until_control_input_and_mic_stays_live(voice):
    actor, socket, asr = voice
    capture = await start_speech(socket, asr)
    connection = asr.connections[-1]
    await connection.events.put({'type': 'partial', 'text': '打开空调'})
    assert (await socket.event('voice.partial'))['capture_id'] == capture
    assert not actor.turns
    await socket.frames(False, 20)
    await socket.event('voice.speech_end')
    await asyncio.wait_for(connection.finished.wait(), 1)
    await connection.events.put({'type': 'final', 'text': '打开空调。'})
    assert (await socket.event('voice.final'))['text'] == '打开空调。'
    await actor.queue.join()
    assert not actor.turns and actor.asr_capture_id == capture
    await actor.post({'type': 'input', 'text': '打开空调。'})
    await actor.queue.join()
    assert actor.current.text == '打开空调。' and actor.asr_capture_id is None
    second = await start_speech(socket, asr)
    assert second != capture and len(asr.connections) == 2


async def test_new_speech_during_old_final_wait_cancels_old_asr_and_keeps_new_lease(voice):
    actor, socket, asr = voice
    first = await start_speech(socket, asr)
    old = asr.connections[-1]
    await socket.frames(False, 20)
    await socket.event('voice.speech_end')
    second = await start_speech(socket, asr)
    await old.events.put({'type': 'final', 'text': '过期识别，不能提交'})
    await actor.queue.join()
    assert first != second and old.closed
    assert actor.asr_capture_id == second and not actor.turns
    assert all(item['type'] != 'voice.final' for item in list(socket.outgoing._queue))


async def test_bad_frame_stops_listener_and_never_dispatches_preview(voice):
    actor, socket, asr = voice
    await start_speech(socket, asr)
    await socket.incoming.put({'type': 'websocket.receive', 'bytes': bytes(100)})
    assert '32ms' in (await socket.event('voice.error'))['message']
    assert not actor.turns


async def test_asr_final_before_endpoint_is_rejected(voice):
    actor, socket, asr = voice
    await start_speech(socket, asr)
    await asr.connections[-1].events.put({'type': 'final', 'text': '过早结果'})
    assert '提前关闭' in (await socket.event('voice.error'))['message']
    assert not actor.turns
