"""Shared explicit fixtures for audio session tests."""

from super_xd.demo.audio.providers import PCM24K_TTS
import asyncio


import json

import pytest

from super_xd.demo.tasks.database import Database

from super_xd.demo.session.actor import SessionActor

from tests.support.session import configure_task_fixtures


def tool(name, args, call_id="audio_call"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class ControlledArk:
    def __init__(self, responses):
        self.responses = list(responses)
        self.delta_sent = asyncio.Event()

    async def stream(self, request):
        response = self.responses.pop(0)
        for text in response.get("deltas", [response.get("content", "")]):
            if text:
                yield {"type": "delta", "text": text}
        self.delta_sent.set()
        if response.get("gate"):
            await response["gate"].wait()
        message = {k: v for k, v in response.items() if k not in {"gate", "deltas"}}
        yield {"type": "message", "message": {"role": "assistant", **message}}


class StreamingTTS:
    output_format = PCM24K_TTS

    def __init__(self, *, finish_gate=None, packets=True, error=None):
        self.calls = []
        self.connected = asyncio.Event()
        self.text_received = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finish_gate = finish_gate
        self.packets = packets
        self.error = error

    async def stream_synthesize(self, texts, voice=None):
        call = []
        self.calls.append(call)
        self.connected.set()
        try:
            async for text in texts:
                call.append(text)
                self.text_received.set()
                if self.error:
                    raise self.error
                # Metadata is diagnostic and must never count as first audio.
                yield {"type": "metrics", "provider_first_packet_ms": 1.5}
                if self.packets:
                    yield {"type": "audio", "audio": b"\x01\x00\x02\x00"}
            if self.finish_gate:
                await self.finish_gate.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


@pytest.fixture
async def audio_actor(tmp_path):
    actors = []

    async def make(responses, tts=None):
        ark, tts = ControlledArk(responses), tts or StreamingTTS()
        actor = SessionActor(
            f"audio{len(actors)}",
            Database(tmp_path / f"{len(actors)}.sqlite3"),
            ark,
            tts,
        )
        configure_task_fixtures(actor)
        actor.simulator.execution_ms = actor.simulator.network_rtt_ms = 0
        actors.append(actor)
        control = await actor.attach()
        await control.get()
        audio = await actor.attach_audio()
        await actor.post({"type": "audio.configure", "enabled": True})
        await actor.queue.join()
        return actor, control, audio, ark, tts

    yield make
    await asyncio.gather(*(actor.close() for actor in actors))


async def until(queue, wanted, timeout=1):
    events = []
    async with asyncio.timeout(timeout):
        while True:
            event = await queue.get()
            events.append(event)
            if (wanted == "pcm" and isinstance(event, bytes)) or (
                isinstance(event, dict) and event.get("type") == wanted
            ):
                return event, events


async def submit(actor, text="陪我聊一句", request="request-a"):
    await actor.post({"type": "input", "text": text, "client_request_id": request})
