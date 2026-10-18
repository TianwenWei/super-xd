"""Shared framing/usage/cancellation mechanisms, with no network or lifecycle state."""
from super_xd.nlp.contracts.providers import ModelResult
from super_xd.nlp.contracts.providers import ModelRequest
from tests.support.models import scripted_result
import asyncio
from copy import deepcopy

import pytest

from super_xd.nlp.engine.models import MalformedModelResponse, TokenUsage, complete_model, extract_usage, normalize_result, open_model_stream


@pytest.mark.parametrize('metadata,expected', [
    ({}, TokenUsage()), ({'usage': None}, TokenUsage()),
    ({'usage': {'prompt_tokens': 7, 'completion_tokens': 2}}, TokenUsage(7, 2, None)),
    ({'usage': {'input_tokens': 3, 'output_tokens': 0, 'total_tokens': 5}}, TokenUsage(3, 0, 5)),
    ({'usage': {'total_tokens': 17}}, TokenUsage(None, None, 17)),
    ({'usage': {'input_tokens': True, 'output_tokens': -1, 'total_tokens': 1.5}}, TokenUsage()),
])
def test_usage_preserves_partial_unknown_and_does_not_estimate_tokens(metadata, expected):
    assert extract_usage(metadata) == expected


def test_normalization_preserves_raw_fields_but_separates_metadata():
    raw = {'role': 'assistant', 'content': 'answer', 'reasoning_content': 'raw reasoning',
           'extension': {'provider': 'field'}, '_meta': {'usage': None, 'raw_response': {'original': True}}}
    before = deepcopy(raw)
    result = normalize_result(scripted_result(raw))
    assert result.message == {key: value for key, value in raw.items() if key != '_meta'}
    assert result.metadata == raw['_meta'] and result.raw_return == {'message': result.message, 'meta': raw['_meta']}
    result.metadata['raw_response']['original'] = False
    result.message['extension']['provider'] = 'changed'
    assert result.raw_return == {'message': {key: value for key, value in before.items() if key != '_meta'}, 'meta': before['_meta']} and raw == before


@pytest.mark.parametrize('raw', [None, [], 'text', {'role': 'user', 'content': 'wrong'},
                                {'role': 'assistant', 'content': []},
                                {'role': 'assistant', '_meta': []},
                                {'role': 'assistant', 'content': float('nan')}])
def test_malformed_return_has_serializable_original_diagnostic(raw):
    import json
    with pytest.raises(MalformedModelResponse) as caught:
        normalize_result(ModelResult(raw))
    json.dumps(caught.value.trace_partial, allow_nan=False)
    assert 'raw_return' in caught.value.trace_partial


async def test_completion_copies_inputs_and_uses_same_response_contract():
    class Model:
        async def complete(self, request):
            messages = request.messages
            tools = request.tools
            options = {key: value for key, value in vars(request).items() if key not in {'messages', 'tools'} and value is not None}
            assert options == {'role': 'talker', 'model': 'provider-model'}
            messages[0]['content'] = 'provider mutation'
            tools.clear()
            return scripted_result({'role': 'assistant', 'content': None, '_meta': {'usage': {'total_tokens': 9}}})
    messages, tools = [{'role': 'user', 'content': 'original'}], [{'type': 'function'}]
    result = await complete_model(Model(), ModelRequest(messages, tools, model='provider-model'))
    assert messages[0]['content'] == 'original' and tools
    assert result.usage == TokenUsage(None, None, 9)


async def test_completion_event_cancellation_waits_for_upstream_cleanup_and_keeps_partial():
    entered, closed, cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Model:
        async def complete(self, request):
            try:
                entered.set()
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                error.trace_partial = {'meta': {'provider_id': 'observed'}}
                raise
            finally:
                closed.set()
    task = asyncio.create_task(complete_model(Model(), ModelRequest([]), cancel=cancel))
    await entered.wait()
    cancel.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert closed.is_set() and caught.value.trace_partial == {'meta': {'provider_id': 'observed'}}


async def test_stream_cancel_closes_upstream_without_late_final_message():
    entered, closed, cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Model:
        async def stream(self, request):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield {'type': 'message', 'message': {'role': 'assistant', 'content': 'late'}}
            finally:
                closed.set()
    async def consume():
        async with open_model_stream(Model(), ModelRequest([], []), cancel=cancel) as stream:
            return [item async for item in stream]
    task = asyncio.create_task(consume())
    await entered.wait()
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.parametrize('items', [
    [], [{'type': 'delta', 'text': 42}],
    [{'type': 'message', 'message': {'role': 'assistant', 'content': 'first'}},
     {'type': 'message', 'message': {'role': 'assistant', 'content': 'second'}}],
    [{'type': 'message', 'message': {'role': 'assistant', 'content': 'first'}},
     {'type': 'delta', 'text': 'late'}],
])
async def test_bad_stream_framing_always_closes_provider(items):
    closed = asyncio.Event()
    class Model:
        async def stream(self, request):
            try:
                for item in items:
                    yield item
            finally:
                closed.set()
    with pytest.raises(MalformedModelResponse):
        async with open_model_stream(Model(), ModelRequest([], [])) as stream:
            [item async for item in stream]
    assert closed.is_set()


async def test_consumer_abandonment_closes_provider_without_consuming_next_item():
    closed, emitted = asyncio.Event(), []
    class Model:
        async def stream(self, request):
            try:
                emitted.append('delta')
                yield {'type': 'delta', 'text': 'partial'}
                emitted.append('final')
                yield {'type': 'message', 'message': {'role': 'assistant', 'content': 'final'}}
            finally:
                closed.set()
    async with open_model_stream(Model(), ModelRequest([], [])) as stream:
        assert (await anext(stream))['text'] == 'partial'
    assert closed.is_set() and emitted == ['delta']


async def test_stream_preserves_provider_model_option_raw_metadata_and_message_separately():
    raw = {'type': 'message', 'message': {'role': 'assistant', 'content': 'answer'},
           'meta': {'provider_id': 'observed', 'usage': {'total_tokens': 12}}, 'provider_extension': {'id': 'raw'}}
    class Model:
        async def stream(self, request):
            options = {key: value for key, value in vars(request).items() if key not in {'messages', 'tools'} and value is not None}
            assert options == {'role': 'talker', 'model': 'provider-model'}
            yield raw
    async with open_model_stream(Model(), ModelRequest([], [], model='provider-model')) as stream:
        items = [item async for item in stream]
    assert items[0]['message'] == {'role': 'assistant', 'content': 'answer'}
    assert items[0]['meta'] == {'provider_id': 'observed', 'usage': {'total_tokens': 12}}
    assert items[0]['raw_return'] == raw


async def test_plain_provider_error_keeps_redacted_diagnostic_reason():
    from super_xd.nlp.engine.run import AgentRun
    from tests.support.agent_run import prepared, collect
    class Model:
        async def stream(self, request):
            raise RuntimeError('Ark HTTP 401; Authorization: Bearer offline-sensitive-value')
            yield
    events = await collect(AgentRun(prepared(), Model()))
    failure = events[-1]
    assert failure.type == 'run.failed' and failure.data['code'] == 'model_or_protocol_error'
    assert 'Ark HTTP 401' in failure.data['message']
    assert 'offline-sensitive-value' not in failure.data['message']
    assert '[redacted]' in failure.data['message']


@pytest.mark.parametrize('trigger', ['cancel', 'deadline'])
async def test_run_interruption_keeps_observed_partial_trace_and_closes_upstream(trigger):
    from super_xd.nlp.contracts.run import RunBudget
    from super_xd.nlp.engine.run import AgentRun
    from tests.support.agent_run import prepared, collect
    entered, closed = asyncio.Event(), asyncio.Event()
    class Model:
        async def stream(self, request):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield
            except asyncio.CancelledError as error:
                error.trace_partial = {'provider_request_id': 'observed-id', 'token': 'private'}
                raise
            finally:
                closed.set()
    budget = RunBudget(active_timeout_seconds=0.01 if trigger == 'deadline' else 60)
    run = AgentRun(prepared(budget=budget), Model())
    job = asyncio.create_task(collect(run))
    await entered.wait()
    if trigger == 'cancel':
        run.cancel()
    events = await job
    assert closed.is_set()
    assert events[-1].type == ('run.cancelled' if trigger == 'cancel' else 'run.failed')
    assert events[-1].data['partial'] == {'provider_request_id': 'observed-id', 'token': '[redacted]'}


@pytest.mark.parametrize('outcome', ['return', 'error'])
async def test_noncooperative_provider_late_outcome_is_diagnostic_only(outcome):
    from super_xd.nlp.engine.run import AgentRun
    from tests.support.agent_run import prepared, collect
    entered, closed = asyncio.Event(), asyncio.Event()
    raw = {'type': 'message', 'message': {'role': 'assistant', 'content': 'late answer'},
           'meta': {'provider_id': 'observed', 'usage': {'total_tokens': 9}}}
    class Model:
        async def stream(self, request):
            try:
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    if outcome == 'error':
                        error = RuntimeError('late transport error Bearer private-token')
                        error.trace_partial = {'provider_request_id': 'observed'}
                        raise error
                    yield raw
            finally:
                closed.set()
    run = AgentRun(prepared(), Model())
    job = asyncio.create_task(collect(run))
    await entered.wait()
    run.cancel()
    events = await job
    assert closed.is_set() and events[-1].type == 'run.cancelled'
    assert not any(event.type in {'text.delta', 'model.completed', 'run.suspended'} for event in events)
    partial = events[-1].data['partial']
    if outcome == 'return':
        assert partial['late_return'] == raw
    else:
        assert partial['late_error']['error_type'] == 'RuntimeError'
        assert partial['late_error']['partial'] == {'provider_request_id': 'observed'}
        assert 'private-token' not in partial['late_error']['message']
