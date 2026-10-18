from super_xd.nlp.prompts.compiler import compile_history_turn
"""Wire recovery, resource reserves and policy feedback on the shared service."""
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from super_xd.nlp.contracts.run import ToolObservation
from super_xd.nlp.contracts.gateway_v1 import MAX_START_BYTES, MAX_WIRE_BYTES, MAX_OBSERVATION_BYTES, StartRequest, ResumeRequest, HistoryTurn, GatewayCheckpoint, GroupCheckpoint
from super_xd.nlp.engine.run import RunProtocolError
from super_xd.nlp.contracts.providers import ProviderError
from tests.support.gateway_contract import (start_request, service, ScriptedModel, call, collect,
                                    resume_request, observation, policy, advertise, binding)


async def test_large_accepted_start_roundtrips_without_duplicate_prefix_or_vehicle():
    value = start_request().model_dump()
    value['context']['vehicle']['state']['_meta']['large_fact'] = 'x' * (MAX_START_BYTES - 8192)
    start = StartRequest.model_validate(value)
    model = ScriptedModel([{'tool_calls': [call('get_weather', {'location': '巴黎', 'date': 'today'})]},
                           {'content': '巴黎的查询结果已收到。'}])
    kernel = service(model)
    first = (await collect(kernel.start(start)))[-1]
    saved = GatewayCheckpoint.model_validate_json(first.checkpoint.model_dump_json())
    assert len(first.model_dump_json().encode()) < MAX_WIRE_BYTES
    assert saved.run.checkpoint_version == 2 and 'prepared' not in saved.run.model_dump()
    assert len(saved.run.messages) == 1 and saved.run.messages[0]['role'] == 'assistant'
    assert saved.orchestration.groups[0].vehicle_state == {}
    assert saved.orchestration.groups[0].vehicle_state_delta is True
    no_result = resume_request(saved)
    assert len(no_result.model_dump_json().encode()) < MAX_WIRE_BYTES
    waiting = (await collect(kernel.resume(no_result)))[-1]
    assert waiting.requests == [] and len(model.requests) == 1
    receipt = observation(saved, ok=True, summary={'temperature_c': 18})
    done = (await collect(kernel.resume(resume_request(waiting.checkpoint, [receipt]))))[-1]
    assert done.type == 'run.completed'
    assert len(done.model_dump_json().encode()) < MAX_WIRE_BYTES
    records = kernel.turn_records(done.checkpoint)
    assert records[0] == {'role': 'user', 'content': start.user_text}
    assert [item['role'] for item in records] == ['user', 'assistant', 'tool', 'assistant']
    assert 'large_fact' not in json.dumps(records)
    assert any('large_fact' in str(item) for item in kernel.transcript(done.checkpoint))


def test_input_and_observation_resource_limits_are_explicit_without_truncation():
    value = start_request().model_dump()
    value['context']['vehicle']['state']['_meta']['large_fact'] = 'x' * MAX_START_BYTES
    with pytest.raises(ValidationError, match='start_size_exceeded'):
        StartRequest.model_validate(value)


async def test_oversized_receipt_is_rejected_before_any_fact_or_model_is_accepted():
    model = ScriptedModel([{'tool_calls': [call()]}])
    kernel = service(model)
    first = (await collect(kernel.start(start_request())))[-1]
    original = first.checkpoint.model_dump_json()
    receipt = observation(first.checkpoint, ok=True, payload='x' * MAX_OBSERVATION_BYTES)
    with pytest.raises(ValidationError, match='observations_size_exceeded'):
        resume_request(first.checkpoint, [receipt])
    assert first.checkpoint.model_dump_json() == original
    assert len(model.requests) == 1
    smaller = observation(first.checkpoint, ok=True, payload='x' * (200 * 1024))
    with pytest.raises(RunProtocolError, match='checkpoint_size_exceeded'):
        kernel.resume(resume_request(first.checkpoint, [smaller]))
    assert len(model.requests) == 1


async def test_correctable_group_rejection_closes_protocol_and_charges_original_budget():
    invalid = call(arguments={'volume': 'loud'})
    model = ScriptedModel([{'tool_calls': [invalid]}, {'tool_calls': [call(identity='fixed')]}])
    kernel = service(model)
    event = (await collect(kernel.start(start_request())))[-1]
    assert event.type == 'run.suspended' and len(event.requests) == 1
    assert event.requests[0].request.tool_call_id == 'fixed'
    assert event.checkpoint.run.model_calls == 2 and event.checkpoint.run.tool_calls == 2
    feedback = json.loads(model.requests[1]['messages'][-1]['content'])
    assert feedback['code'] == 'invalid_tool_arguments' and feedback['executed'] is False
    assert feedback['retry_model'] is True
    assert feedback['parameter_path'] == ['volume'] and feedback['validator'] == 'type'
    assert feedback['expected']['type'] in ('number', 'integer')


async def test_source_repair_and_unavailable_capability_have_distinct_outcomes():
    source = advertise(start_request(), 'set_navigation_destination')
    model = ScriptedModel([{'tool_calls': [call('set_navigation_destination', {'destination': '不存在的地点'})]},
                           {'content': '你想导航到哪里？'}])
    result = (await collect(service(model, semantic_policy=policy(set_navigation_destination=binding())).start(source)))[-1]
    assert result.type == 'run.completed' and len(model.requests) == 2
    assert not result.requests
    unavailable = start_request()
    unavailable.context.capabilities.tools = []
    model = ScriptedModel([{'tool_calls': [call()]}])
    result = (await collect(service(model).start(unavailable)))[-1]
    assert result.type == 'run.failed' and result.error_code == 'unsupported_capability'
    assert result.error_details['code'] == 'unsupported_capability'
    assert result.error_details['executed'] is False and result.error_details['reason']
    assert len(model.requests) == 1


async def test_provider_failure_preserves_diagnostics_but_not_in_display_output():
    class BrokenModel:
        async def stream(self, *args, **kwargs):
            error = ProviderError('account_blocked', 'Provider rejected request', provider='fixture',
                                  details={'status_code': 403, 'request_id': 'r1'})
            error.trace_partial = {'message': {'role': 'assistant', 'content': 'partial'}}
            raise error
            yield
    class Sink:
        def __init__(self): self.records = []
        async def emit(self, record): self.records.append(record)
    sink = Sink()
    result = (await collect(service(BrokenModel(), diagnostics=sink).start(start_request())))[-1]
    failure = next(item.event for item in sink.records if item.event and item.event.type == 'run.failed')
    assert failure.data['provider_error']['code'] == 'account_blocked'
    assert failure.data['diagnostics']['status_code'] == 403
    assert failure.data['partial']['message']['content'] == 'partial'
    assert result.error_code == 'account_blocked' and result.error_details == {}
    assert 'request_id' not in result.error_details


async def test_retrieval_progress_survives_unknown_and_prevents_repeat_handoffs():
    query = {'tool_calls': [call('get_weather', {'location': '巴黎', 'date': 'today'})]}
    model = ScriptedModel([query, query, query, {'content': '只能确认已有的天气资料。'}])
    kernel = service(model)
    first = (await collect(kernel.start(start_request())))[-1]
    unknown = observation(first.checkpoint, status='unknown', ok=False)
    pending = (await collect(kernel.resume(resume_request(first.checkpoint, [unknown]))))[-1]
    assert not pending.checkpoint.orchestration.retrieval.records and not pending.requests
    result = {'ok': True, 'provider': 'fixture', 'location': '巴黎', 'date': 'today', 'summary': {'temperature_c': 18}}
    second = (await collect(kernel.resume(resume_request(pending.checkpoint, [observation(pending.checkpoint, **result)]))))[-1]
    assert second.type == 'run.suspended' and len(second.checkpoint.orchestration.retrieval.records) == 1
    done = (await collect(kernel.resume(resume_request(second.checkpoint, [observation(second.checkpoint, **result)]))))[-1]
    assert done.type == 'run.completed' and len(done.checkpoint.orchestration.retrieval.records) == 2
    assert len(model.requests) == 4
    feedback = json.loads(model.requests[-1]['messages'][-1]['content'])
    assert feedback['code'] == 'retrieval_progress_exhausted'
    actual_query = json.loads(model.requests[1]['messages'][-1]['content'])
    assert actual_query['answer_support_assessed'] is False and actual_query['evidence_refs']
    # Raw accepted facts stay unannotated and exact for policy replay.
    raw = json.loads(second.checkpoint.run.messages[-2]['content'])
    assert raw == result
    altered = second.checkpoint.model_copy(deep=True)
    altered.orchestration.retrieval.records = []
    with pytest.raises(RunProtocolError, match='Retrieval progress'):
        kernel.resume(resume_request(altered))


def test_history_playback_keeps_generated_and_heard_text_separate():
    turn = HistoryTurn(turn_id='old', records=[{'role': 'assistant', 'content': '完整生成答复'}],
                       heard_text='完整', playback_status='partial', interrupted_playback=True)
    context = compile_history_turn(turn)
    assert context.messages[0]['content'] == '完整生成答复' and context.heard_text == '完整'
    with pytest.raises(ValidationError):
        HistoryTurn(turn_id='old', records=[{'role': 'assistant', 'content': '答复'}],
                    heard_text='答', playback_status='not_started')


async def test_malformed_json_and_unknown_function_are_explicit_terminal_errors():
    malformed = call()
    malformed['function']['arguments'] = '{broken'
    for proposed, code in ((malformed, 'invalid_tool_arguments'), (call('not_a_tool'), 'unknown_tool')):
        model = ScriptedModel([{'tool_calls': [proposed]}])
        kernel = service(model)
        event = (await collect(kernel.start(start_request())))[-1]
        assert event.type == 'run.failed' and event.error_code == code
        assert event.error_details['code'] == code and event.error_details['retry_model'] is False
        assert event.checkpoint.run.pending == [] and len(model.requests) == 1
        assert event.checkpoint.run.messages == []
        assert event.presentation.source == 'error_template'
        assert kernel.turn_records(event.checkpoint) == [
            {'role': 'user', 'content': start_request().user_text},
            {'role': 'assistant', 'content': event.presentation.speech}]


async def test_repair_budget_exhaustion_is_specific_and_never_dispatches_invalid_group():
    model = ScriptedModel([{'tool_calls': [call(arguments={'volume': 101})]}] * 2)
    start = start_request()
    start.budget.max_model_calls = 2
    events = await collect(service(model).start(start))
    assert events[-1].type == 'run.failed'
    assert events[-1].error_code == 'model_call_budget_exhausted'
    assert not any(item.requests for item in events)
    assert events[-1].checkpoint.run.model_calls == 2
    assert events[-1].checkpoint.run.tool_calls == 2
    feedback = json.loads(model.requests[1]['messages'][-1]['content'])
    assert feedback['validator'] == 'maximum' and feedback['expected']['maximum'] == 100
