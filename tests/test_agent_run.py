"""NLP-only protocol tests: no actor, database, device or external provider."""
import asyncio
import json

import pytest
from pydantic import ValidationError

from backend.harness.contracts import PreparedRun, RunIdentity, RunBudget, ToolObservation
from backend.harness.run import AgentRun, RunProtocolError


def prepared(**changes):
    values = dict(identity=RunIdentity(run_id='run', request_id='request', turn_id='turn', context_revision=3),
                  messages=[{'role': 'user', 'content': 'explicit offline input'}], release_id='release-a',
                  tools=[{'type': 'function', 'function': {'name': 'lookup', 'parameters': {
                      'type': 'object', 'properties': {'city': {'type': 'string'}}, 'required': ['city'],
                      'additionalProperties': False}}}])
    values.update(changes)
    return PreparedRun(**values)


def call(city='Paris', identifier='call'):
    return {'id': identifier, 'type': 'function', 'function': {'name': 'lookup', 'arguments': json.dumps({'city': city})}}


class ScriptedModel:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    async def stream(self, messages, tools, **options):
        self.requests.append({'messages': messages, 'tools': tools, 'options': options})
        response = self.responses.pop(0)
        if response.get('content'):
            yield {'type': 'delta', 'text': response['content']}
        yield {'type': 'message', 'message': {'role': 'assistant', **response}, 'meta': {'fixture': True}}


async def collect(run, observations=()):
    return [event async for event in run.advance(observations)]


def observation(run, request, status='completed', **result):
    return ToolObservation(identity=run.prepared.identity, invocation_id=request.invocation_id,
                           status=status, result=result or {'ok': True, 'value': 'verified fixture'})


async def test_plain_answer_streams_and_needs_no_external_services():
    model = ScriptedModel([{'content': 'hello'}])
    run = AgentRun(prepared(), model)
    events = await collect(run)
    assert [event.type for event in events] == ['model.started', 'text.delta', 'model.completed', 'run.completed']
    assert run.state.output == 'hello'
    assert len(model.requests) == 1
    assert run.state.status == 'completed'
    with pytest.raises(RunProtocolError):
        await collect(run)


async def test_checkpoint_roundtrip_resumes_on_new_instance_with_complete_tool_group():
    model = ScriptedModel([{'tool_calls': [call('Paris', 'a'), call('Lyon', 'b')]}, {'content': 'verified answer'}])
    first = AgentRun(prepared(), model)
    events = await collect(first)
    assert len(events[-1].data['requests']) == 2
    checkpoint = json.loads(first.checkpoint().model_dump_json())
    second = AgentRun(prepared(), model, checkpoint=checkpoint)
    requests = second.state.pending
    result_b = observation(second, requests[1])
    partial = await collect(second, [result_b, result_b])
    assert [event.type for event in partial] == ['run.suspended']
    assert len(model.requests) == 1
    resumed = AgentRun(prepared(), model, checkpoint=json.loads(second.checkpoint().model_dump_json()))
    final = await collect(resumed, [observation(resumed, requests[0])])
    assert final[-1].type == 'run.completed'
    messages = model.requests[-1]['messages']
    assert [item['tool_call_id'] for item in messages if item['role'] == 'tool'] == ['a', 'b']
    assert resumed.state.model_calls == 2 and resumed.state.tool_calls == 2
    assert final[0].sequence > partial[-1].sequence


async def test_unknown_result_waits_for_reconciliation_without_reissuing_tool():
    model = ScriptedModel([{'tool_calls': [call()]}, {'content': 'confirmed'}])
    run = AgentRun(prepared(), model)
    await collect(run)
    request = run.state.pending[0]
    events = await collect(run, [observation(run, request, 'unknown', reason='receipt unavailable')])
    assert events[-1].data['status'] == 'waiting_reconciliation'
    again = await collect(run)
    assert [event.type for event in again] == ['run.suspended']
    assert len(model.requests) == 1
    final = await collect(run, [observation(run, request)])
    assert final[-1].type == 'run.completed'


async def test_rejected_observation_cannot_claim_ok_in_model_context():
    model = ScriptedModel([{'tool_calls': [call()]}, {'content': 'rejected'}])
    run = AgentRun(prepared(), model)
    await collect(run)
    await collect(run, [observation(run, run.state.pending[0], 'rejected', ok=True, code='not_allowed')])
    assert json.loads(model.requests[-1]['messages'][-1]['content'])['ok'] is False


async def test_invalid_identity_is_rejected_atomically_and_correct_result_can_resume():
    model = ScriptedModel([{'tool_calls': [call()]}, {'content': 'answer'}])
    run = AgentRun(prepared(), model)
    await collect(run)
    request = run.state.pending[0]
    valid = observation(run, request)
    old = valid.model_copy(update={'identity': valid.identity.model_copy(update={'context_revision': 2})})
    before = run.checkpoint()
    with pytest.raises(RunProtocolError):
        await collect(run, [valid, old])
    assert run.checkpoint() == before
    assert (await collect(run, [valid]))[-1].type == 'run.completed'


async def test_conflicting_duplicate_result_and_changed_release_are_rejected():
    model = ScriptedModel([{'tool_calls': [call('Paris', 'a'), call('Lyon', 'b')]}])
    run = AgentRun(prepared(), model)
    await collect(run)
    first = observation(run, run.state.pending[0])
    await collect(run, [first])
    with pytest.raises(RunProtocolError):
        await collect(run, [first.model_copy(update={'result': {'other': True}})])
    with pytest.raises(RunProtocolError):
        AgentRun(prepared(release_id='release-b'), model, checkpoint=run.checkpoint())


async def test_call_budget_is_preserved_across_resume():
    model = ScriptedModel([{'tool_calls': [call()]}, {'content': 'must not be called'}])
    input_ = prepared(budget=RunBudget(max_model_calls=1))
    first = AgentRun(input_, model)
    await collect(first)
    resumed = AgentRun(input_, model, checkpoint=first.checkpoint())
    events = await collect(resumed, [observation(resumed, resumed.state.pending[0])])
    assert events[-1].type == 'run.failed'
    assert events[-1].data['code'] == 'model_call_budget_exhausted'
    assert len(model.requests) == 1


async def test_bad_second_call_prevents_entire_group_from_being_exposed():
    bad = call('Lyon', 'b')
    bad['function']['arguments'] = '{"city":42}'
    run = AgentRun(prepared(), ScriptedModel([{'tool_calls': [call('Paris', 'a'), bad]}]))
    events = await collect(run)
    assert not any(event.data.get('requests') for event in events)
    assert events[-1].type == 'run.failed'


async def test_product_validation_runs_before_any_request_is_exposed():
    def policy(requests):
        assert len(requests) == 2
        raise ValueError('explicit offline semantic policy rejection')
    run = AgentRun(prepared(), ScriptedModel([{'tool_calls': [call('Paris', 'a'), call('Lyon', 'b')]}]), validate_group=policy)
    events = await collect(run)
    assert not any(event.data.get('requests') for event in events)
    assert events[-1].type == 'run.failed'


async def test_cancellation_interrupts_blocked_model_stream_and_closes_it():
    entered, closed = asyncio.Event(), asyncio.Event()
    class BlockingModel:
        async def stream(self, messages, tools, **options):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield {'type': 'delta', 'text': 'must never appear'}
            finally:
                closed.set()
    run = AgentRun(prepared(), BlockingModel())
    job = asyncio.create_task(collect(run))
    await entered.wait()
    run.cancel()
    events = await asyncio.wait_for(job, 1)
    assert closed.is_set()
    assert events[-1].type == 'run.cancelled'
    assert not any(event.type == 'text.delta' or event.data.get('requests') for event in events)


async def test_cancel_after_started_event_does_not_start_provider_request():
    model = ScriptedModel([{'content': 'must not start'}])
    run = AgentRun(prepared(), model)
    stream = run.advance()
    assert (await anext(stream)).type == 'model.started'
    run.cancel()
    events = [event async for event in stream]
    assert [event.type for event in events] == ['run.cancelled']
    assert model.requests == []


async def test_provider_timeout_is_distinct_from_cumulative_model_budget():
    class TimedOutProvider:
        async def stream(self, messages, tools, **options):
            raise TimeoutError('provider deadline')
            yield
    run = AgentRun(prepared(), TimedOutProvider())
    events = await collect(run)
    assert events[-1].data['code'] == 'model_timeout'
    assert run.checkpoint().active_seconds < run.prepared.budget.active_timeout_seconds


async def test_active_timeout_is_bounded_and_does_not_reset_in_checkpoint():
    class SlowModel:
        async def stream(self, messages, tools, **options):
            await asyncio.sleep(1)
            yield {'type': 'delta', 'text': 'too late'}
    run = AgentRun(prepared(budget=RunBudget(active_timeout_seconds=0.01)), SlowModel())
    events = await collect(run)
    assert events[-1].data['code'] == 'active_time_budget_exhausted'
    assert run.checkpoint().active_seconds >= 0.01


async def test_consumer_backpressure_does_not_consume_model_budget_or_cancel_consumer():
    run = AgentRun(prepared(budget=RunBudget(active_timeout_seconds=0.1)),
                   ScriptedModel([{'content': 'answer'}]))
    events = []
    async for event in run.advance():
        events.append(event)
        if event.type in {'model.started', 'text.delta'}:
            await asyncio.sleep(0.15)
    assert events[-1].type == 'run.completed'
    assert run.checkpoint().active_seconds < 0.1


async def test_model_time_budget_remains_cumulative_after_checkpoint():
    initial = prepared(budget=RunBudget(active_timeout_seconds=0.1))
    first = AgentRun(initial, ScriptedModel([{'tool_calls': [call()]}]))
    await collect(first)
    saved = first.checkpoint()
    saved.active_seconds = 0.09
    class SlowContinuation:
        async def stream(self, messages, tools, **options):
            await asyncio.sleep(0.04)
            yield {'type': 'message', 'message': {'role': 'assistant', 'content': 'late'}}
    resumed = AgentRun(initial, SlowContinuation(), checkpoint=saved)
    events = await collect(resumed, [observation(resumed, resumed.state.pending[0])])
    assert events[-1].data['code'] == 'active_time_budget_exhausted'


def test_contract_rejects_unknown_version_nan_and_runtime_handles():
    with pytest.raises(ValidationError):
        prepared(protocol_version='future')
    with pytest.raises((ValidationError, ValueError)):
        prepared(messages=[{'value': float('nan')}])
    with pytest.raises((ValidationError, TypeError)):
        prepared(messages=[{'value': asyncio.Event()}])


async def test_suspension_delivers_whole_group_and_checkpoint_as_one_handoff():
    model = ScriptedModel([{'tool_calls': [call('Paris', 'a'), call('Lyon', 'b')]}])
    run = AgentRun(prepared(), model)
    stream = run.advance()
    async for event in stream:
        if event.type == 'run.suspended':
            handoff = event.data
            break
    await stream.aclose()
    assert len(handoff['requests']) == 2
    assert handoff['requests'] == handoff['checkpoint']['pending']
    resumed = AgentRun(prepared(), model, checkpoint=handoff['checkpoint'])
    events = await collect(resumed)
    assert events[-1].data['requests'] == []
    assert len(model.requests) == 1


async def test_cancel_while_consuming_suspension_preserves_pending_audit_and_terminates():
    model = ScriptedModel([{'tool_calls': [call()]}])
    run = AgentRun(prepared(), model)
    events = []
    async for event in run.advance():
        events.append(event)
        if event.type == 'run.suspended':
            run.cancel()
    assert events[-1].type == 'run.cancelled'
    assert run.checkpoint().pending
    assert run.checkpoint().status == 'cancelled'
    assert len(model.requests) == 1


async def test_stored_observation_revision_and_request_payload_are_validated():
    run = AgentRun(prepared(), ScriptedModel([{'tool_calls': [call()]}]))
    await collect(run)
    result = observation(run, run.state.pending[0]).model_dump()
    result['identity']['context_revision'] = 2
    checkpoint = run.checkpoint().model_dump()
    checkpoint['observations'][result['invocation_id']] = result
    with pytest.raises(RunProtocolError):
        AgentRun(prepared(), run.model, checkpoint=checkpoint)
    checkpoint = run.checkpoint().model_dump()
    checkpoint['pending'][0]['arguments']['city'] = 'unrequested city'
    with pytest.raises(RunProtocolError):
        AgentRun(prepared(), run.model, checkpoint=checkpoint)
