"""Empty native arguments are normalized once, before replay and persistence."""
from copy import deepcopy
import json

import httpx
import pytest

from super_xd.demo.devices.simulator import execute_tool
from super_xd.demo.gateway import history_records
from super_xd.nlp.adapters.models.service import ArkTransport, ChatProvider, ModelService
from super_xd.nlp.config.services import ServiceConfig
from super_xd.nlp.contracts.gateway_v1 import HistoryTurn
from super_xd.nlp.contracts.providers import ModelResult
from super_xd.nlp.engine.models import normalize_result
from tests.support.gateway_diagnostics import BatchDiagnostics
from tests.support.model_trace import Frames, sse
from tests.support.orchestration import StreamModel, call, kernel, outputs, request, resume
from tests.support.service_config import document


OPAQUE = {'google': {'thought_signature': 'offline-provider-signature+/=='}}


def empty_call(encoding, *, name='close_all_windows', identity='close'):
    result = call(name, {}, identity)
    result['extra_content'] = deepcopy(OPAQUE)
    if encoding == 'missing':
        result['function'].pop('arguments')
    else:
        result['function']['arguments'] = encoding
    return result


@pytest.mark.parametrize('encoding', ['', 'missing', '{}'])
def test_model_acceptance_preserves_raw_empty_arguments_and_opaque_metadata(encoding):
    native = {'role': 'assistant', 'content': None, 'tool_calls': [empty_call(encoding)],
              'extra_content': deepcopy(OPAQUE)}
    original = deepcopy(native)
    result = normalize_result(ModelResult(native, {'provider_id': 'offline'}))
    assert result.message['tool_calls'][0]['function']['arguments'] == '{}'
    assert result.message['tool_calls'][0]['extra_content'] == OPAQUE
    assert result.message['extra_content'] == OPAQUE
    assert result.raw_return['message'] == original == native


@pytest.mark.parametrize('encoding', ['', 'missing', '{}'])
@pytest.mark.parametrize('fast_path', [True, False])
async def test_empty_sse_arguments_survive_receipt_recovery_presentation_and_history(monkeypatch, encoding, fast_path):
    monkeypatch.setenv('EMPTY_ARGUMENT_TEST_KEY', 'offline-key')
    profile = {'provider': 'ark', 'key_env': 'EMPTY_ARGUMENT_TEST_KEY'}
    proposed = empty_call(encoding)
    chunks = [sse({'choices': [{'delta': {'content': '正在关闭车窗。', 'extra_content': OPAQUE,
               'tool_calls': [{'index': 0, **proposed}]}, 'finish_reason': 'tool_calls'}]}), b'data: [DONE]\n\n']
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, stream=Frames(chunks))
        return httpx.Response(200, stream=Frames([sse({'choices': [{'delta': {
            'content': '车窗已经关闭。'}, 'finish_reason': 'stop'}]}), b'data: [DONE]\n\n']))

    async with httpx.AsyncClient(base_url='https://offline.test/', transport=httpx.MockTransport(respond)) as client:
        provider = ChatProvider(profile, transport=ArkTransport(profile, client))
        config = ServiceConfig(document())
        model = ModelService(config, providers={name: provider for name in config.document['llm']['profiles']}, owner=False)
        start = request('关闭所有车窗', tools=[('close_all_windows', {}, 'control', 'device')])
        diagnostics = BatchDiagnostics()
        service = kernel(model, start, diagnostics=diagnostics, receipt_fast_path=fast_path)
        first = (await outputs(service.start(start)))[-1]
        assert first.type == 'run.suspended'
        assert len(first.requests) == 1 and first.requests[0].request.arguments == {}
        native = first.checkpoint.run.messages[-1]
        assert native['tool_calls'][0]['function']['arguments'] == '{}'
        assert native['extra_content'] == native['tool_calls'][0]['extra_content'] == OPAQUE
        vehicle = deepcopy(start.context.vehicle.state)
        execute_tool('close_all_windows', {}, vehicle)
        done = (await outputs(resume(service, first, {'ok': True, 'executed': True,
                                                     'vehicle_state': vehicle})))[-1]
        assert done.type == 'run.completed' and done.presentation is None
        assert len(requests) == (1 if fast_path else 2)
        if not fast_path:
            continued = next(row for row in requests[1]['messages'] if row.get('tool_calls')
                             and row['tool_calls'][0]['id'] == 'close')
            assert continued == native
        history = HistoryTurn(turn_id='previous', records=history_records(service.turn_records(done.checkpoint)))
        historical = next(row for row in history.records if row.role == 'assistant' and row.tool_calls)
        assert historical.tool_calls[0].arguments == {}
        assert historical.tool_calls[0].extra_content == OPAQUE
        completed = next(record.event.data for record in diagnostics.records
                         if record.kind == 'run.event' and record.event.type == 'model.completed')
        raw = completed['raw_return']['message']['tool_calls'][0]
        assert raw['function']['arguments'] == ('' if encoding == 'missing' else encoding)
        assert raw['extra_content'] == OPAQUE


@pytest.mark.parametrize('encoding', ['', 'missing', '{}'])
async def test_required_argument_rejection_keeps_whole_group_replayable(encoding):
    invalid = empty_call(encoding, name='set_temperature', identity='temperature')
    no_args = empty_call(encoding)
    model = StreamModel({'tool_calls': [invalid, no_args]}, {'content': '温度需要明确后才能调整。'})
    start = request('调整温度然后关闭车窗', tools=[('set_temperature', {}, 'control', 'device'),
                                                ('close_all_windows', {}, 'control', 'device')])
    service = kernel(model, start)
    events = await outputs(service.start(start))
    assert events[-1].type == 'run.completed' and not any(event.requests for event in events)
    assert len(model.inputs) == 2
    proposed = next(row for row in model.inputs[1] if row.get('tool_calls')
                    and row['tool_calls'][0]['id'] == 'temperature')
    assert [item['function']['arguments'] for item in proposed['tool_calls']] == ['{}', '{}']
    assert all(item['extra_content'] == OPAQUE for item in proposed['tool_calls'])
    feedback = [json.loads(row['content']) for row in model.inputs[1][-2:]]
    assert all(row['code'] == 'invalid_tool_arguments' and row['validator'] == 'required' for row in feedback)
    assert events[-1].checkpoint.run.model_calls == events[-1].checkpoint.run.tool_calls == 2


@pytest.mark.parametrize('arguments', ['{', 'not-json', ' ', 'null', '[]', '0', 'false', '""', None, False, 0, [], {}])
async def test_invalid_nonempty_or_nonobject_arguments_never_become_empty_object(arguments):
    proposed = call('close_all_windows', {})
    proposed['function']['arguments'] = arguments
    start = request('关闭所有车窗', tools=[('close_all_windows', {}, 'control', 'device')])
    events = await outputs(kernel(StreamModel({'tool_calls': [proposed]}), start).start(start))
    assert events[-1].type == 'run.failed' and events[-1].error_code == 'invalid_tool_arguments'
    assert not any(event.requests for event in events)
