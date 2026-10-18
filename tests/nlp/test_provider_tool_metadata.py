from super_xd.nlp.contracts.providers import ModelRequest
from super_xd.nlp.prompts.compiler import compile_history_turn
"""Continuation metadata survives streaming, recovery and factual history."""
from copy import deepcopy
import json

import httpx
import pytest
from pydantic import ValidationError

from super_xd.demo.gateway import history_records
from super_xd.nlp.contracts.gateway_v1 import GatewayCheckpoint, HistoricalCall, HistoryTurn
from super_xd.nlp.adapters.models.service import ArkTransport
from tests.support.gateway_contract import (ScriptedModel, call, collect, observation,
                                   resume_request, service, start_request)
from tests.support.model_trace import Frames, sse


SIGNATURE = 'opaque-provider-continuation+/=='
EXTRA = {'google': {'thought_signature': SIGNATURE}}


async def test_stream_metadata_survives_argument_deltas_and_is_sent_on_continuation(monkeypatch):
    monkeypatch.setenv('ARK_API_KEY', 'offline')
    chunks = [
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'a', 'type': 'function',
          'function': {'name': 'get_weather', 'arguments': '{"location":'}, 'extra_content': EXTRA}]}}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '"巴黎"}'},
          'extra_content': {'google': {'other': 'value'}}}]}, 'finish_reason': 'tool_calls'}]},
    ]
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(200, stream=Frames([*(sse(c) for c in chunks), b'data: [DONE]\n\n']))
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': '结果已收到。'}}]})

    async with httpx.AsyncClient(base_url='https://provider.test/', transport=httpx.MockTransport(respond)) as client:
        model = ArkTransport({'provider': 'ark', 'key_env': 'ARK_API_KEY', 'timeout_seconds': 45}, client)
        items = [item async for item in model.stream(ModelRequest([{'role': 'user', 'content': '查天气'}], model='fixture-model'))]
        message = items[-1]['message']
        assert message['tool_calls'][0]['extra_content'] == {'google': {'thought_signature': SIGNATURE, 'other': 'value'}}
        await model.complete(ModelRequest([message, {'role': 'tool', 'tool_call_id': 'a', 'content': '{}'}], model='fixture-model'))
    assert requests[1]['messages'][0] == message
    assert message['tool_calls'][0]['function']['arguments'] == '{"location":"巴黎"}'


async def test_conflicting_stream_signature_is_rejected_without_rewriting_partial(monkeypatch):
    monkeypatch.setenv('ARK_API_KEY', 'offline')
    frames = [sse({'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'a',
               'extra_content': {'google': {'thought_signature': value}}}]}}]})
              for value in (SIGNATURE, 'conflicting-value')]
    async with httpx.AsyncClient(base_url='https://provider.test/', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Frames(frames)))) as client:
        with pytest.raises(RuntimeError, match='Conflicting provider tool metadata') as caught:
            _ = [item async for item in ArkTransport({'provider': 'ark', 'key_env': 'ARK_API_KEY', 'timeout_seconds': 45}, client).stream(ModelRequest([{'role': 'user', 'content': '查天气'}], model='fixture-model'))]
    assert caught.value.trace_partial['message']['tool_calls'][0]['extra_content'] == EXTRA


async def test_checkpoint_receipt_resume_and_new_turn_preserve_opaque_metadata():
    proposed = {**call('get_weather', {'location': '巴黎', 'date': 'today'}), 'extra_content': deepcopy(EXTRA)}
    model = ScriptedModel([{'tool_calls': [proposed]}, {'content': '巴黎天气已收到。'}])
    kernel = service(model)
    first = (await collect(kernel.start(start_request())))[-1]
    saved = GatewayCheckpoint.model_validate_json(first.checkpoint.model_dump_json())
    done = (await collect(kernel.resume(resume_request(saved, [observation(saved, ok=True, summary={'temperature_c': 18})]))))[-1]
    assert done.type == 'run.completed'
    actual = next(m for m in model.requests[-1]['messages'] if any(c.get('extra_content') for c in m.get('tool_calls', [])))
    assert actual['tool_calls'][0]['extra_content'] == EXTRA
    records = history_records(kernel.turn_records(done.checkpoint))
    history = HistoryTurn(turn_id='previous', records=records)
    roundtrip = HistoryTurn.model_validate_json(history.model_dump_json())
    native = compile_history_turn(roundtrip).messages
    assert next(m for m in native if m.get('tool_calls'))['tool_calls'][0]['extra_content'] == EXTRA


def test_history_metadata_rejects_overflow_instead_of_truncating():
    value = {'id': 'a', 'name': 'get_weather', 'arguments': {}, 'extra_content': {'google': {'thought_signature': 'x' * 65536}}}
    with pytest.raises(ValidationError, match='64 KiB'):
        HistoricalCall.model_validate(value)


@pytest.mark.parametrize('conflicting', [False, True])
async def test_top_level_continuation_metadata_is_atomic_and_not_concatenated(monkeypatch, conflicting):
    monkeypatch.setenv('ARK_API_KEY', 'offline')
    first = {'google': {'thought_signature': SIGNATURE}}
    following = {'google': {'thought_signature': 'changed' if conflicting else SIGNATURE, 'other': 'value'}}
    chunks = [sse({'choices': [{'delta': {'extra_content': value}, 'finish_reason': 'stop' if index else None}]})
              for index, value in enumerate((first, following))]
    async with httpx.AsyncClient(base_url='https://provider.test/', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Frames(chunks + [b'data: [DONE]\n\n'])))) as client:
        model = ArkTransport({'provider': 'ark', 'key_env': 'ARK_API_KEY'}, client)
        if conflicting:
            with pytest.raises(RuntimeError, match='Conflicting provider tool metadata') as error:
                _ = [item async for item in model.stream(ModelRequest([], model='fixture-model'))]
            assert error.value.trace_partial['message']['extra_content'] == first
        else:
            final = [item async for item in model.stream(ModelRequest([], model='fixture-model'))][-1]
            assert final['message']['extra_content'] == following


async def test_top_level_metadata_round_trips_checkpoint_history_and_next_model_request():
    proposed = {'tool_calls': [call('get_weather', {'location': '巴黎', 'date': 'today'})],
                'extra_content': deepcopy(EXTRA)}
    model = ScriptedModel([proposed, {'content': '巴黎天气已收到。'}])
    kernel = service(model)
    first = (await collect(kernel.start(start_request())))[-1]
    saved = GatewayCheckpoint.model_validate_json(first.checkpoint.model_dump_json())
    done = (await collect(kernel.resume(resume_request(saved, [observation(saved, ok=True,
        summary={'temperature_c': 18})]))))[-1]
    assert done.type == 'run.completed'
    actual = next(message for message in model.requests[-1]['messages']
                  if any(item.get('id') == proposed['tool_calls'][0]['id'] for item in message.get('tool_calls', [])))
    assert actual['extra_content'] == EXTRA
    records = history_records(kernel.turn_records(done.checkpoint))
    roundtrip = HistoryTurn.model_validate_json(HistoryTurn(turn_id='previous', records=records).model_dump_json())
    native = compile_history_turn(roundtrip).messages
    assert next(message for message in native if message.get('tool_calls'))['extra_content'] == EXTRA
