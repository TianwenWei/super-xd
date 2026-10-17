"""Continuation metadata survives streaming, recovery and factual history."""
from copy import deepcopy
import json

import httpx
import pytest
from pydantic import ValidationError

from backend.demo.local_gateway import history_records
from backend.harness.gateway_contracts import GatewayCheckpoint, HistoricalCall, HistoryTurn
from backend.models import ArkClient
from test_gateway_contract import (ScriptedModel, call, collect, observation,
                                   resume_request, service, start_request)
from test_model_trace import Frames, sse


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
        model = ArkClient(client)
        items = [item async for item in model.stream([{'role': 'user', 'content': '查天气'}])]
        message = items[-1]['message']
        assert message['tool_calls'][0]['extra_content'] == {'google': {'thought_signature': SIGNATURE, 'other': 'value'}}
        await model.complete([message, {'role': 'tool', 'tool_call_id': 'a', 'content': '{}'}])
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
            _ = [item async for item in ArkClient(client).stream([{'role': 'user', 'content': '查天气'}])]
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
    native = roundtrip.context_turn().messages
    assert next(m for m in native if m.get('tool_calls'))['tool_calls'][0]['extra_content'] == EXTRA


def test_history_metadata_rejects_overflow_instead_of_truncating():
    value = {'id': 'a', 'name': 'get_weather', 'arguments': {}, 'extra_content': {'google': {'thought_signature': 'x' * 65536}}}
    with pytest.raises(ValidationError, match='64 KiB'):
        HistoricalCall.model_validate(value)
