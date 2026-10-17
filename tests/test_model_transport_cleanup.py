"""A stalled transport close cannot suppress the original timeout or cancellation."""
import asyncio
import time

import httpx
import pytest

from backend.models import ArkClient, _CallBudget, _bounded_response


class BlockedClose(httpx.AsyncByteStream):
    def __init__(self):
        self.reading = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_finished = asyncio.Event()

    async def __aiter__(self):
        self.reading.set()
        await asyncio.Event().wait()
        yield b''

    async def aclose(self):
        self.close_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.close_finished.set()


@pytest.mark.parametrize('streaming', [True, False])
@pytest.mark.parametrize('cancelled', [True, False])
async def test_stalled_response_close_preserves_original_failure_and_releases_cooperative_task(monkeypatch, streaming, cancelled):
    monkeypatch.setenv('ARK_API_KEY', 'offline-cleanup-fixture')
    wire = BlockedClose()
    async with httpx.AsyncClient(base_url='https://offline.test/', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=wire))) as client:
        model = ArkClient(client)

        async def request():
            if streaming:
                return [item async for item in model.stream([], total_timeout_seconds=1 if cancelled else .01)]
            return await model.complete([], total_timeout_seconds=1 if cancelled else .01)

        task = asyncio.create_task(request())
        await wire.reading.wait()
        started = time.perf_counter()
        if cancelled:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError) as failure:
            await asyncio.wait_for(task, .5)
        assert time.perf_counter() - started < .35
        assert wire.close_started.is_set()
        await asyncio.wait_for(wire.close_finished.wait(), .2)
        meta = failure.value.trace_partial['meta']
        assert 'cleanup exceeded' in meta['stream_close_error']
        if not cancelled:
            assert failure.value.provider_code == 'timeout'
            assert meta['deadline_phase'] == 'total'
        if streaming:
            assert len(meta['attempts']) == 1
            assert meta['attempts'][0]['status'] == ('cancelled' if cancelled else 'failed')


async def test_noncooperative_cleanup_is_detached_without_replacing_original_error():
    release, finished = asyncio.Event(), asyncio.Event()

    class Context:
        async def __aenter__(self):
            raise ValueError('original provider failure')

        async def __aexit__(self, *args):
            try:
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
            finally:
                finished.set()

    class Client:
        def stream(self, *args, **kwargs):
            return Context()

    try:
        async with asyncio.timeout(.5):
            with pytest.raises(ValueError, match='original provider failure') as failure:
                async with _bounded_response(Client(), _CallBudget(1)):
                    pytest.fail('Failed enter must never yield a response')
        assert 'cleanup exceeded' in failure.value.stream_close_error
        assert not finished.is_set()
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), .5)
