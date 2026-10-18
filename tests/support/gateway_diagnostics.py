"""Shared explicit fixtures for gateway diagnostics tests."""

import asyncio


class BatchDiagnostics:
    def __init__(self):
        self.batches = []

    @property
    def records(self):
        return [record for batch in self.batches for record in batch]

    async def emit_many(self, records):
        self.batches.append(list(records))


class ChunkModel:
    def __init__(self, pieces, *, block=False, calls=()):
        self.pieces, self.block, self.calls = pieces, block, calls
        self.closed, self.waiting = asyncio.Event(), asyncio.Event()

    async def stream(self, *args, **kwargs):
        try:
            for piece in self.pieces:
                yield {"type": "delta", "text": piece}
            if self.block:
                self.waiting.set()
                await asyncio.Event().wait()
            yield {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": "".join(self.pieces),
                    "tool_calls": list(self.calls),
                },
                "meta": {"offline": True},
            }
        finally:
            self.closed.set()
