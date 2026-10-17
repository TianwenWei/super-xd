"""Terminal speech is a shared expression of accepted facts, never execution."""
from copy import deepcopy
import json

import pytest

from backend.cockpit import initial_cockpit_state
from backend.demo.local_gateway import history_records
from backend.demo.vehicle_simulator import execute_tool
from backend.harness.contracts import ToolObservation
from backend.harness.gateway_contracts import GatewayCheckpoint, HistoryTurn
from backend.harness.presentation import completion_presentation
from backend.harness.product_policy import ProductPolicyError
from test_gateway_contract import (ScriptedModel, call, collect, observation, policy,
                                   resume_request, service, start_request)
from test_orchestration import request, kernel, outputs, resume, StreamModel, message
from test_session import make_actor, wait_for


async def volume_receipt(*, persona='professional', preface=None, fast=True):
    model = ScriptedModel([{'content': preface, 'tool_calls': [call()]}, {'content': '音量已调整。'}])
    input_ = start_request(persona_id=persona)
    core = service(model, receipt_fast_path=fast)
    first = (await collect(core.start(input_)))[-1]
    assert first.presentation is None
    done = (await collect(core.resume(resume_request(first.checkpoint, [observation(first.checkpoint,
        ok=True, executed=True, vehicle_state={'_meta': {'speed_kph': 0}, 'media': {'volume': 35}})]))))[-1]
    return core, model, done


async def test_tool_only_completion_uses_actual_value_and_separate_committed_presentation():
    core, model, done = await volume_receipt()
    assert done.type == 'run.completed' and len(model.requests) == 1
    assert done.presentation.source == 'verified_receipts'
    assert '35' in done.presentation.speech and '40' not in done.presentation.speech
    assert done.presentation == done.checkpoint.presentation
    saved = GatewayCheckpoint.model_validate_json(done.checkpoint.model_dump_json())
    assert core.transcript(saved)[-1]['role'] == 'tool'
    records = core.turn_records(saved)
    assert records[-1] == {'role': 'assistant', 'content': done.presentation.speech}
    HistoryTurn(turn_id='previous', records=history_records(records))
    assert model.requests[0]['messages'][-1]['content'].endswith(start_request().user_text)


@pytest.mark.parametrize('preface,fast', [('正在调整音量。', True), (None, False)])
async def test_current_control_preface_or_final_answer_needs_no_extra_speech(preface, fast):
    _, model, done = await volume_receipt(preface=preface, fast=fast)
    assert done.type == 'run.completed' and done.presentation is None
    assert len(model.requests) == (1 if fast else 2)


async def test_prior_query_preface_does_not_suppress_later_tool_only_control():
    model = ScriptedModel([{'content': '正在查询天气。', 'tool_calls': [call('get_weather',
        {'location': '巴黎', 'date': 'today'}, 'weather')]}, {'tool_calls': [call()]}])
    core = service(model)
    first = (await collect(core.start(start_request())))[-1]
    second = (await collect(core.resume(resume_request(first.checkpoint, [observation(first.checkpoint,
        ok=True, location='巴黎', summary={'temperature_c': 18})]))))[-1]
    done = (await collect(core.resume(resume_request(second.checkpoint, [observation(second.checkpoint)]))))[-1]
    assert done.type == 'run.completed' and len(model.requests) == 2
    assert '40' in done.presentation.speech and '天气' not in done.presentation.speech


@pytest.mark.parametrize('first_preface,last_preface', [
    (None, None), ('正在调整音量并查询天气。', None),
    (None, '正在打开空调。'), ('正在调整音量并查询天气。', '正在打开空调。'),
])
async def test_terminal_aggregates_all_unannounced_groups_and_prefaces_cover_only_their_own_group(first_preface, last_preface):
    input_ = request('音量调到40并查巴黎天气，然后打开空调', tools=[
        ('set_volume', {}, 'control', 'device'), ('get_weather', {}, 'query', 'cloud'),
        ('set_climate_power', {}, 'control', 'device')])
    input_.context.cockpit = {}
    model = StreamModel({'content': first_preface, 'tool_calls': [call(),
        call('get_weather', {'location': '巴黎', 'date': 'today'}, 'weather')]},
        {'content': last_preface, 'tool_calls': [call('set_climate_power', {'enabled': True})]})
    core = kernel(model, input_)
    first = (await outputs(core.start(input_)))[-1]
    vehicle = deepcopy(input_.context.vehicle.state)
    execute_tool('set_volume', {'volume': 40}, vehicle)
    accepted = [ToolObservation(identity=first.identity, invocation_id=item.request.invocation_id,
        status='completed', result=({'ok': True, 'executed': True, 'vehicle_state': deepcopy(vehicle)}
        if item.request.name == 'set_volume' else {'ok': True, 'summary': {'temperature_c': 18}}))
        for item in first.requests]
    second = (await outputs(core.resume(resume_request(first.checkpoint, accepted))))[-1]
    execute_tool('set_climate_power', {'enabled': True}, vehicle)
    done = (await outputs(resume(core, second, {'ok': True, 'executed': True, 'vehicle_state': vehicle})))[-1]
    assert done.type == 'run.completed' and len(model.inputs) == 2
    assert len(done.checkpoint.orchestration.groups) == 2
    speech = done.presentation.speech if done.presentation else ''
    assert ('40' in speech) is (first_preface is None)
    assert ('空调' in speech) is (last_preface is None)
    assert '18' not in speech and '天气' not in speech
    assert speech.count('40') <= 1 and speech.count('空调') <= 1


async def test_reused_call_ids_and_identical_arguments_do_not_transfer_an_earlier_preface():
    model = ScriptedModel([{'content': '正在调整音量并查询天气。', 'tool_calls': [call(),
        call('get_weather', {'location': '巴黎', 'date': 'today'}, 'weather')]}, {'tool_calls': [call()]}])
    core = service(model)
    first = (await collect(core.start(start_request())))[-1]
    second = (await collect(core.resume(resume_request(first.checkpoint, [observation(first.checkpoint),
        observation(first.checkpoint, 1, ok=True, summary={'temperature_c': 18})]))))[-1]
    done = (await collect(core.resume(resume_request(second.checkpoint, [observation(second.checkpoint)]))))[-1]
    assert len(model.requests) == 2 and done.type == 'run.completed'
    assert done.presentation.speech.count('40') == 1
    groups = done.checkpoint.orchestration.groups
    assert groups[0].requests[0].request.tool_call_id == groups[1].requests[0].request.tool_call_id
    assert groups[0].requests[0].request.invocation_id != groups[1].requests[0].request.invocation_id


@pytest.mark.parametrize('arguments,tool_content', [
    ('{broken', None), ('[]', None), ('null', None), (None, '{broken'), (None, '[]'),
])
async def test_malformed_native_payload_cannot_crash_presentation_or_hide_actual_receipts(arguments, tool_content):
    _, _, done = await volume_receipt()
    checkpoint = done.checkpoint.model_copy(deep=True)
    proposal, receipt = checkpoint.run.messages
    proposal['content'] = '原提案预告。'
    if arguments is not None:
        proposal['tool_calls'][0]['function']['arguments'] = arguments
    if tool_content is not None:
        receipt['content'] = tool_content
    # Only the accepted orchestration receipt grants completion; malformed
    # native audit data cannot grant preface suppression or cause a new action.
    presentation = completion_presentation(checkpoint)
    assert '35' in presentation.speech and '40' not in presentation.speech


async def test_persona_changes_only_rendering_of_same_actual_control():
    results = [await volume_receipt(persona=persona) for persona in ('professional', 'warm', 'playful')]
    assert len({done.presentation.speech for _, _, done in results}) == 3
    assert all('35' in done.presentation.speech for _, _, done in results)
    assert all(len(model.requests) == 1 for _, model, _ in results)
    assert len({json.dumps(done.checkpoint.orchestration.model_dump(), sort_keys=True) for _, _, done in results}) == 1


async def test_model_failure_reports_completed_control_but_not_successful_query_as_execution():
    model = ScriptedModel([{'tool_calls': [call(), call('get_weather', {'location': '巴黎', 'date': 'today'}, 'weather')]}])
    core = service(model)
    first = (await collect(core.start(start_request())))[-1]
    done = (await collect(core.resume(resume_request(first.checkpoint, [observation(first.checkpoint),
        observation(first.checkpoint, 1, ok=True, executed=True, summary={'temperature_c': 18})]))))[-1]
    assert done.type == 'run.failed' and done.error_code == 'model_or_protocol_error'
    assert '40' in done.presentation.speech and '连接' in done.presentation.speech
    assert '18' not in done.presentation.speech and '天气' not in done.presentation.speech
    assert 'IndexError' not in done.presentation.speech
    assert len(model.requests) == 2


async def test_unknown_and_cancelled_never_claim_completion():
    core = service(ScriptedModel([{'tool_calls': [call()]}]))
    first = (await collect(core.start(start_request())))[-1]
    waiting = (await collect(core.resume(resume_request(first.checkpoint,
        [observation(first.checkpoint, status='unknown', ok=True, executed=True)]))))[-1]
    assert waiting.presentation is None and completion_presentation(waiting.checkpoint) is None
    cancelled = (await collect(core.resume(resume_request(waiting.checkpoint, cancel=True))))[-1]
    assert cancelled.type == 'run.cancelled' and cancelled.presentation is None


async def test_capability_failure_uses_chinese_template_and_structured_spoken_reason_has_priority():
    input_ = start_request()
    input_.context.capabilities.tools = []
    done = (await collect(service(ScriptedModel([{'tool_calls': [call()]}])).start(input_)))[-1]
    assert done.error_code == 'unsupported_capability'
    assert done.error_details['reason'] not in done.presentation.speech
    assert '连接' not in done.presentation.speech and '没有提供这项能力' in done.presentation.speech

    class Refusal(type(policy())):
        def validate_group(self, input_):
            raise ProductPolicyError('unsupported_capability', 'private provider detail',
                                     spoken_reason='此设备暂不支持该功能。', retry_model=False)
    core = service(ScriptedModel([{'tool_calls': [call()]}]), semantic_policy=Refusal({}))
    refused = (await collect(core.start(start_request())))[-1]
    assert '此设备暂不支持该功能' in refused.presentation.speech
    assert 'private' not in refused.presentation.speech


async def test_diagnostic_failure_preserves_pending_protocol_and_separate_presentation():
    class FailingSink:
        async def emit(self, record):
            if record.event and record.event.type == 'run.suspended':
                raise RuntimeError('offline commit failed')
    core = service(ScriptedModel([{'tool_calls': [call()]}]), diagnostics=FailingSink())
    done = (await collect(core.start(start_request())))[-1]
    assert done.error_code == 'diagnostics_unavailable' and done.presentation is not None
    assert done.checkpoint.run.pending
    records = core.turn_records(done.checkpoint)
    assert records[-1]['tool_calls'] and records[-1].get('content') is None
    HistoryTurn(turn_id='previous', records=history_records(records), pending_tool_group=True)


@pytest.mark.parametrize('name,args,domain', [
    ('control_media', {'action': 'volume', 'volume': 40}, 'media'),
    ('manage_navigation', {'action': 'cancel'}, 'navigation'),
])
async def test_media_and_navigation_terminal_speech_uses_accepted_device_receipt(name, args, domain):
    input_ = request('调整当前设备', tools=[(name, args, 'control', 'device')])
    model = StreamModel(message(name, args))
    core = kernel(model, input_)
    first = (await outputs(core.start(input_)))[-1]
    state = deepcopy(initial_cockpit_state()[domain])
    state.update(version=1)
    if domain == 'media':
        state['volume'] = 40
    done = (await outputs(resume(core, first, {'ok': True, 'executed': True, 'domain': domain,
                                             'state': state, 'state_version': 1})))[-1]
    assert done.type == 'run.completed' and len(model.inputs) == 1
    assert ('播放器' if domain == 'media' else '导航设备') in done.presentation.speech
    assert '模拟' not in done.presentation.speech


async def test_task_registration_is_spoken_only_after_ledger_ack():
    from backend.harness.background import TaskService
    from evals.gateway_driver import MemoryTaskLedger
    input_ = request('调查公开资料')
    model = StreamModel(message('delegate_task', {'goal': '核实公开资料', 'spoken_goal': '核实资料'}))
    ledger = MemoryTaskLedger()
    tasks = TaskService(ledger, lambda store: None, model_policy_id='offline-policy')
    core = kernel(model, input_, task_service=tasks)
    done = (await outputs(core.start(input_)))[-1]
    assert done.type == 'run.completed' and len(ledger.tasks) == 1 and len(model.inputs) == 1
    assert '核实资料' in done.presentation.speech
    result = next(iter(done.checkpoint.orchestration.groups[0].results.values())).result
    assert result['registered'] is True and result['executed'] is False


async def test_task_commit_unknown_is_not_announced_as_registered():
    class Uncertain(RuntimeError):
        uncertain = True
    class UnavailableLedger:
        async def handle(self, *args):
            raise Uncertain('no durable acknowledgement')
    input_ = request('调查公开资料')
    core = kernel(StreamModel(message('delegate_task', {'goal': '核实公开资料'})), input_,
                  task_service=UnavailableLedger())
    done = (await outputs(core.start(input_)))[-1]
    assert done.error_code == 'task_commit_unknown'
    assert done.presentation.source == 'error_template'
    assert '尚未确认' in done.presentation.speech and '核实公开资料' not in done.presentation.speech


async def test_demo_consumes_exact_core_presentation_and_trace_without_extra_model(make_actor):
    actor, queue, model = await make_actor([{'tool_calls': [call()]}])
    await actor.post({'type': 'input', 'text': '把音量调到40'})
    done, _ = await wait_for(queue, 'stream.done', actor)
    turn = actor.turns[-1]
    assert turn.checkpoint.presentation.speech == turn.output == done['text']
    assert turn.messages[-1]['content'] == turn.output
    assert turn.metrics['output_source'] == 'verified_receipts'
    assert done['metrics']['model_calls'] == len(model.requests) == 1
