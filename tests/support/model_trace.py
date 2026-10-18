"""Shared explicit fixtures for model trace tests."""

import asyncio

import json

import httpx

import pytest


def sse(body):
    return ("data: " + json.dumps(body, ensure_ascii=False) + "\n\n").encode()


class Frames(httpx.AsyncByteStream):
    def __init__(self, frames, blocked=False):
        self.frames, self.blocked, self.closed = frames, blocked, False

    async def __aiter__(self):
        for frame in self.frames:
            yield frame
        if self.blocked:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "fake-transport-only-credential")
