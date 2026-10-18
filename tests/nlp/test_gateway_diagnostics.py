"""Bounded trace batches preserve every delta and acknowledge before handoff."""
import asyncio
import json

import httpx
import pytest

from super_xd.nlp.adapters.gateway.diagnostics import GatewayDiagnostics
from super_xd.nlp.diagnostics import safe_diagnostic
from super_xd.nlp.adapters.gateway.tasks import GatewayTaskStore
from tests.support.gateway_contract import call, collect, service, start_request


from tests.support.gateway_diagnostics import (
    BatchDiagnostics, ChunkModel
)


@pytest.mark.parametrize('count,expected_batches', [(10, 3), (140, 5)])
async def test_delta_batching_reduces_writes_without_losing_order_or_model_metadata(count, expected_batches):
    sink = BatchDiagnostics()
    run = service(ChunkModel([str(index) for index in range(count)]), diagnostics=sink).start(start_request())
    output = await collect(run)
    assert output[-1].type == 'run.completed'
    assert len(sink.batches) == expected_batches
    deltas = [record.event.data['text'] for record in sink.records if record.event.type == 'text.delta']
    assert deltas == [str(index) for index in range(count)]
    assert [record.event.sequence for record in sink.records] == sorted(record.event.sequence for record in sink.records)
    completed = next(record.event for record in sink.records if record.event.type == 'model.completed')
    assert completed.data['metadata'] == {'offline': True}
    assert not run._diagnostic_pending


async def test_large_delta_flushes_without_waiting_for_completion():
    sink = BatchDiagnostics()
    run = service(ChunkModel(['中' * 11000], block=True), diagnostics=sink).start(start_request())
    events = run.events()
    await anext(events)
    assert [record.event.type for record in sink.records] == ['model.started', 'text.delta']
    await events.aclose()
    assert sink.records[-1].event.type == 'run.cancelled'


async def test_handoff_waits_for_atomic_batch_ack_and_failure_withholds_every_request():
    ready, acknowledge = asyncio.Event(), asyncio.Event()

    class Slow(BatchDiagnostics):
        async def emit_many(self, records):
            if records[-1].event.type == 'model.completed':
                ready.set()
                await acknowledge.wait()
            await super().emit_many(records)

    sink = Slow()
    run = service(ChunkModel(['准备调整'], calls=[call()]), diagnostics=sink).start(start_request())
    events = run.events()
    assert (await anext(events)).type == 'text.delta'
    next_output = asyncio.create_task(anext(events))
    await asyncio.wait_for(ready.wait(), 1)
    assert not next_output.done()
    acknowledge.set()
    assert (await asyncio.wait_for(next_output, 1)).type == 'run.suspended'
    assert sink.records[-1].event.type == 'run.suspended'
    await events.aclose()
    assert run.checkpoint().run.status == 'waiting_tools'

    class Failing(BatchDiagnostics):
        async def emit_many(self, records):
            if records[-1].event.type == 'model.completed':
                raise RuntimeError('failed durable commit')
            await super().emit_many(records)

    run = service(ChunkModel(['准备调整'], calls=[call()]), diagnostics=Failing()).start(start_request())
    output = await collect(run)
    assert not any(event.requests for event in output)
    assert output[-1].error_code == 'diagnostics_unavailable'
    assert output[-1].checkpoint.run.status == 'failed'
    assert not run._diagnostic_pending


async def test_consumer_close_flushes_partial_output_and_closes_upstream():
    sink, model = BatchDiagnostics(), ChunkModel(['部分内容'], block=True)
    run = service(model, diagnostics=sink).start(start_request())
    events = run.events()
    assert (await anext(events)).text == '部分内容'
    assert [record.event.type for record in sink.records] == ['model.started']
    await events.aclose()
    assert model.closed.is_set()
    assert [record.event.type for record in sink.records] == ['model.started', 'text.delta', 'run.cancelled']
    assert not run._diagnostic_pending


@pytest.mark.parametrize('external', [True, False])
async def test_cancellation_flushes_partial_output(external):
    sink, model = BatchDiagnostics(), ChunkModel(['部分内容'], block=True)
    run = service(model, diagnostics=sink).start(start_request())
    running = asyncio.create_task(collect(run))
    await asyncio.wait_for(model.waiting.wait(), 1)
    if external:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    else:
        run.cancel()
        assert (await asyncio.wait_for(running, 1))[-1].type == 'run.cancelled'
    assert model.closed.is_set()
    assert [record.event.type for record in sink.records] == ['model.started', 'text.delta', 'run.cancelled']
    assert not run._diagnostic_pending


async def test_cancellation_waits_for_inflight_commit_without_replaying_it():
    committing, acknowledge = asyncio.Event(), asyncio.Event()

    class Slow(BatchDiagnostics):
        async def emit_many(self, records):
            if any(record.event.type == 'text.delta' for record in records):
                committing.set()
                await acknowledge.wait()
            await super().emit_many(records)

    sink = Slow()
    run = service(ChunkModel(['中' * 11000], block=True), diagnostics=sink).start(start_request())
    running = asyncio.create_task(collect(run))
    await asyncio.wait_for(committing.wait(), 1)
    running.cancel()
    await asyncio.sleep(0)
    assert not running.done()
    acknowledge.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 1)
    assert [record.event.type for record in sink.records] == ['model.started', 'text.delta', 'run.cancelled']
    assert not run._diagnostic_pending


async def test_remote_batches_have_stable_keys_and_redact_before_transport():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'accepted': True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = GatewayTaskStore('https://gateway.example', token='private-token', client=client)
        diagnostics = GatewayDiagnostics(transport)
        run = service(ChunkModel(['private-token']), diagnostics=diagnostics).start(start_request())
        assert (await collect(run))[-1].type == 'run.completed'
    assert len(bodies) == 3
    assert all(body['idempotency_key'].startswith('diagnostics-') for body in bodies)
    assert len({body['idempotency_key'] for body in bodies}) == 3
    assert 'private-token' not in json.dumps(bodies)
    records = [record for body in bodies for record in body['records']]
    assert [record['event']['type'] for record in records] == ['model.started', 'text.delta', 'model.completed', 'run.completed']


def test_schema_dict_values_do_not_break_media_redaction():
    value = {'properties': {'name': {'type': 'string'}, 'kind': {'enum': ['music', 'video']}}}
    assert safe_diagnostic(value) == value
