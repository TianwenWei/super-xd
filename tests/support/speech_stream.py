"""Explicit speech protocol fixtures shared by Demo tests."""

import asyncio

import gzip

import json

import struct


from super_xd.demo.audio.speech_stream import decode_frame

from super_xd.demo.audio.volcengine_tts import VolcanoTTS


def server_frame(
    event, data=None, *, session="session", audio=None, error=None, compressed=False
):
    body = (
        audio
        if audio is not None
        else json.dumps(data or {}, ensure_ascii=False).encode()
    )
    if compressed:
        body = gzip.compress(body)
    header = bytes(
        [
            0x11,
            0xF0 if error is not None else (0xB4 if audio is not None else 0x94),
            (0 if audio is not None else 0x10) | int(compressed),
            0,
        ]
    )
    prefix = struct.pack(">I", error if error is not None else event)
    if error is None:
        identifier = ("connection" if event < 100 else session).encode()
        prefix += struct.pack(">I", len(identifier)) + identifier
    return header + prefix + struct.pack(">I", len(body)) + body


class FakeWS:
    def __init__(self, *, on_text=None, finish_audio=None):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.on_text = on_text
        self.finish_audio = finish_audio
        self.session = None

    async def send(self, raw):
        frame = decode_frame(raw)
        self.sent.append(frame)
        event = frame["event"]
        if event == 1:
            self.incoming.put_nowait(server_frame(50))
        elif event == 100:
            self.session = frame["identifier"]
            self.incoming.put_nowait(server_frame(150, session=self.session))
        elif event == 200:
            if self.on_text:
                await self.on_text(self, frame["data"]["req_params"]["text"])
            else:
                self.incoming.put_nowait(
                    server_frame(352, session=self.session, audio=b"\x01\x00" * 12)
                )
        elif event == 102:
            if self.finish_audio is not None:
                self.incoming.put_nowait(
                    server_frame(352, session=self.session, audio=self.finish_audio)
                )
            self.incoming.put_nowait(
                server_frame(152, {"usage": {"text_words": 10}}, session=self.session)
            )

    async def recv(self):
        return await self.incoming.get()

    async def close(self):
        self.closed = True


def adapter(ws, *, timeout=1):
    calls = []

    async def connect(url, **kwargs):
        calls.append((url, kwargs))
        return ws

    return VolcanoTTS(
        api_key="offline-speech-secret", ws_connect=connect, timeout_seconds=timeout
    ), calls


async def strings(*parts):
    for part in parts:
        yield part


async def _consume(stream):
    return [item async for item in stream]
