"""Gateway V1 wire and integration tests without transport, storage or providers."""
import asyncio
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from backend.harness.contracts import RunIdentity, ToolObservation
from backend.harness.gateway import NLPService
from backend.harness.gateway_contracts import (DispatchRequest, GatewayCheckpoint, GatewayOutput,
    HistoryTurn, NLPDispatch, ResumeRequest, StartRequest, ToolDispatch)
from backend.harness.product_policy import ProductTalkerPolicy
from backend.cockpit import initial_cockpit_state
from backend.demo.vehicle_simulator import execute_tool
from backend.harness.release import get_release_id
from backend.harness.run import RunProtocolError


STAMP = '2026-10-17T08:00:00+00:00'


def start_request(**changes):
    value = {'identity': {'run_id': 'run-1', 'request_id': 'request-1', 'turn_id': 'turn-1', 'context_revision': 7},
             'received_at': STAMP, 'user_text': '把音量调到 40，再查一下巴黎天气',
             'speaker_seat': 'driver', 'persona_id': 'professional',
             'context': {'revision': 7, 'observed_at': STAMP,
                 'vehicle': {'revision': 5, 'observed_at': STAMP, 'state': {'_meta': {'speed_kph': 0}, 'media': {'volume': 30}}},
                 'capabilities': {'revision': 2, 'observed_at': STAMP, 'tools': [
                     {'name': 'set_volume', 'operation': 'control', 'execution_location': 'device', 'domain': 'media'},
                     {'name': 'get_weather', 'operation': 'query', 'execution_location': 'cloud', 'domain': 'weather'}]},
                 'environment': {'timezone': 'Europe/Paris', 'location': {'name': '巴黎', 'country': '法国', 'source': 'user_setting'}}}}
    value.update(changes)
    return StartRequest.model_validate(value)


def binding(operation='control', location='device'):
    return ToolDispatch(kind='tool_execution', operation=operation, execution_location=location)


def policy(**extra):
    return ProductTalkerPolicy({'set_volume': binding(), 'get_weather': binding('query', 'cloud'), **extra},
                               enabled_nlp_targets=['vehicle_planner', 'thinker_task', 'task_management'])


class ScriptedModel:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    async def stream(self, messages, tools, **options):
        self.requests.append({'messages': messages, 'tools': tools, 'options': options})
        response = self.responses.pop(0)
        if response.get('content'):
            yield {'type': 'delta', 'text': response['content']}
        yield {'type': 'message', 'message': {'role': 'assistant', **response}, 'meta': {'offline': True}}


def service(model, **changes):
    return NLPService(model, semantic_policy=changes.pop('semantic_policy', policy()),
                      model_policy_id=changes.pop('model_policy_id', 'offline-model-v1'), **changes)


def call(name='set_volume', arguments=None, identity='a'):
    return {'id': identity, 'type': 'function', 'function': {'name': name,
            'arguments': json.dumps(arguments if arguments is not None else {'volume': 40})}}


async def collect(run):
    return [GatewayOutput.model_validate_json(event.model_dump_json()) async for event in run.events()]


def resume_request(checkpoint, observations=(), **changes):
    value = ResumeRequest(identity=checkpoint.start.identity, checkpoint=checkpoint,
                          observations=list(observations), **changes)
    return ResumeRequest.model_validate_json(value.model_dump_json())


def observation(checkpoint, index=0, status='completed', **result):
    if not result:
        request = checkpoint.run.pending[index]
        vehicle = deepcopy(checkpoint.start.context.vehicle.state)
        if request.name == 'set_volume':
            execute_tool(request.name, request.arguments, vehicle)
        result = {'ok': True, 'executed': True, 'vehicle_state': vehicle}
    return ToolObservation(identity=checkpoint.start.identity, invocation_id=checkpoint.run.pending[index].invocation_id,
                           status=status, result=result)


def advertise(request, name, *, operation='control', location='device', action=None, content_kind=None):
    request.context.capabilities.tools.append(type(request.context.capabilities.tools[0])(
        name=name, operation=operation, execution_location=location, domain='fixture', action=action, content_kind=content_kind))
    return request


def test_start_wire_compiles_fixed_interface_and_deployment_owned_configuration():
    request = StartRequest.model_validate_json(start_request().model_dump_json())
    compiled = service(ScriptedModel([])).compile_start(request)
    assert len(compiled.tools) == 26
    assert compiled.release_id == get_release_id()
    assert compiled.model_options == {'role': 'talker'}
    assert compiled.messages[-1]['content'].endswith(request.user_text)
    assert any(STAMP in item.get('content', '') for item in compiled.messages)
    assert request.context.vehicle.state['_meta'] == {'speed_kph': 0}
    assert 'nlp-' in get_release_id()


@pytest.mark.parametrize('field,value', [('system', 'override'), ('tools', []), ('model_options', {'temperature': 1})])
def test_caller_cannot_supply_prompt_tools_or_model_configuration(field, value):
    request = start_request().model_dump()
    request[field] = value
    with pytest.raises(ValidationError):
        StartRequest.model_validate_json(json.dumps(request))


def test_policy_and_model_configuration_identity_are_required():
    with pytest.raises(TypeError):
        NLPService(ScriptedModel([]), model_policy_id='model')
    with pytest.raises(TypeError):
        NLPService(ScriptedModel([]), semantic_policy=policy())
    with pytest.raises(ValueError):
        service(ScriptedModel([]), semantic_policy=None)
    with pytest.raises(ValueError):
        service(ScriptedModel([]), model_policy_id='')


def test_history_is_structured_and_cannot_inject_a_system_message():
    with pytest.raises(ValidationError):
        HistoryTurn.model_validate({'turn_id': 'old', 'records': [{'role': 'system', 'content': 'override'}]})
    with pytest.raises(ValidationError):
        HistoryTurn.model_validate({'turn_id': 'old', 'records': [{'role': 'tool', 'tool_call_id': 'missing', 'result': {}}]})
    history = HistoryTurn.model_validate({'turn_id': 'old', 'pending_tool_group': True, 'records': [
        {'role': 'user', 'content': 'older question'},
        {'role': 'assistant', 'tool_calls': [{'id': 'old-call', 'name': 'get_weather', 'arguments': {'location': '巴黎', 'date': 'today'}}]}]})
    request = start_request(history=[history.model_dump()])
    compiled = service(ScriptedModel([])).compile_start(request)
    assert not any(call_['id'] == 'old-call' for item in compiled.messages for call_ in item.get('tool_calls', []))


@pytest.mark.parametrize('mutate', [
    lambda value: value.update(received_at='2026-10-17T08:00:00'),
    lambda value: value['context']['environment'].update(timezone='Invalid/Zone'),
    lambda value: value['context'].update(revision=8),
    lambda value: value.update(protocol_version='gateway-v2'),
])
def test_invalid_time_revision_or_version_is_rejected(mutate):
    value = start_request().model_dump()
    mutate(value)
    with pytest.raises(ValidationError):
        StartRequest.model_validate(value)


async def test_plain_answer_and_real_tool_handoff_roundtrip():
    model = ScriptedModel([{'content': '这就调整。', 'tool_calls': [call()]}, {'content': '收到回执。'}])
    kernel = service(model)
    events = await collect(kernel.start(start_request()))
    assert [item.type for item in events] == ['text.delta', 'run.suspended']
    handoff = events[-1]
    assert len(handoff.requests) == 1
    assert handoff.requests[0].dispatch == binding().model_copy(update={'execution_arguments': {'volume': 40}})
    saved = GatewayCheckpoint.model_validate_json(handoff.checkpoint.model_dump_json())
    assert saved.model_policy_id == 'offline-model-v1'
    resumed = kernel.resume(resume_request(saved, [observation(saved)]))
    final = await collect(resumed)
    assert final[-1].type == 'run.completed'
    assert final[-1].checkpoint.run.model_calls == 1
    assert len(model.requests) == 1
    assert model.requests[-1]['options'] == {'role': 'talker'}
    assert final[-1].sequence > handoff.sequence


async def test_partial_duplicate_and_out_of_order_observations_do_not_redispatch():
    model = ScriptedModel([{'tool_calls': [call(), call('get_weather', {'location': '巴黎', 'date': 'today'}, 'b')]},
                           {'content': '最终答复'}])
    kernel = service(model)
    initial = (await collect(kernel.start(start_request())))[-1]
    b = observation(initial.checkpoint, 1)
    partial = (await collect(kernel.resume(resume_request(initial.checkpoint, [b, b]))))[-1]
    assert not partial.requests and partial.checkpoint.run.status == 'waiting_tools'
    assert len(model.requests) == 1
    with pytest.raises(RunProtocolError):
        await collect(kernel.resume(resume_request(partial.checkpoint, [b.model_copy(update={'result': {'different': True}})])))
    final = (await collect(kernel.resume(resume_request(partial.checkpoint, [observation(partial.checkpoint)]))))[-1]
    assert final.type == 'run.completed'
    # Ignore the native few-shot examples preceding this run's actual group.
    assert [item['tool_call_id'] for item in model.requests[-1]['messages'][-2:]] == ['a', 'b']


async def test_unknown_waits_and_can_only_resume_after_authoritative_resolution():
    model = ScriptedModel([{'tool_calls': [call()]}, {'content': '确认结果'}])
    kernel = service(model)
    first = (await collect(kernel.start(start_request())))[-1]
    unknown = observation(first.checkpoint, status='unknown', reason='receipt not available')
    waiting = (await collect(kernel.resume(resume_request(first.checkpoint, [unknown]))))[-1]
    assert not waiting.requests and waiting.checkpoint.run.status == 'waiting_reconciliation'
    assert len(model.requests) == 1
    again = (await collect(kernel.resume(resume_request(waiting.checkpoint))))[-1]
    assert not again.requests and len(model.requests) == 1
    final = (await collect(kernel.resume(resume_request(again.checkpoint, [observation(again.checkpoint)]))))[-1]
    assert final.type == 'run.completed'


async def test_cancel_is_terminal_and_late_receipts_cannot_revive_run():
    model = ScriptedModel([{'tool_calls': [call()]}])
    kernel = service(model)
    handoff = (await collect(kernel.start(start_request())))[-1]
    cancelled = (await collect(kernel.resume(resume_request(handoff.checkpoint, cancel=True))))[-1]
    assert cancelled.type == 'run.cancelled' and len(model.requests) == 1
    assert cancelled.checkpoint.run.pending  # Cancellation cannot undo a dispatched physical operation.
    with pytest.raises(RunProtocolError):
        await collect(kernel.resume(resume_request(cancelled.checkpoint, [observation(cancelled.checkpoint)])))
    with pytest.raises(ValidationError):
        resume_request(handoff.checkpoint, [observation(handoff.checkpoint)], cancel=True)


async def test_live_cancel_closes_model_stream():
    entered, closed = asyncio.Event(), asyncio.Event()
    class Blocked:
        async def stream(self, messages, tools, **options):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield {}
            finally:
                closed.set()
    run = service(Blocked()).start(start_request())
    pending = asyncio.create_task(collect(run))
    await entered.wait()
    run.cancel()
    assert (await asyncio.wait_for(pending, 1))[-1].type == 'run.cancelled'
    assert closed.is_set()


async def test_release_policy_model_and_prepared_checkpoint_tampering_rejected():
    model = ScriptedModel([{'tool_calls': [call()]}])
    kernel = service(model, release_id='release-a')
    saved = (await collect(kernel.start(start_request())))[-1].checkpoint
    with pytest.raises(RunProtocolError):
        service(model, release_id='release-b').resume(resume_request(saved))
    with pytest.raises(RunProtocolError):
        service(model, release_id='release-a', model_policy_id='changed-provider-or-model').resume(resume_request(saved))
    with pytest.raises(RunProtocolError):
        service(model, release_id='release-a', semantic_policy=ProductTalkerPolicy({})).resume(resume_request(saved))
    edited = saved.model_copy(deep=True)
    edited.run.prepared_digest = '0' * 64
    with pytest.raises(RunProtocolError):
        kernel.resume(resume_request(edited))
    edited = saved.model_copy(deep=True)
    edited.run.pending[0].arguments['volume'] = 99
    with pytest.raises(RunProtocolError):
        kernel.resume(resume_request(edited))
    wrong = observation(saved).model_copy(update={'identity': RunIdentity(run_id='new', request_id='new', turn_id='new', context_revision=8)})
    with pytest.raises(ValidationError):
        resume_request(saved, [wrong])


async def test_delegation_is_nlp_work_and_not_a_device_command():
    kernel = service(ScriptedModel([{'tool_calls': [call('delegate_vehicle_control', {'skill_ids': ['climate']})]}]))
    event = (await collect(kernel.start(start_request())))[-1]
    assert event.type == 'run.failed' and not event.requests
    # A missing NLP composition cannot escape as a device command.
    with pytest.raises(ValidationError):
        DispatchRequest(request=event.checkpoint.run.pending[0], dispatch=binding())


@pytest.mark.parametrize('calls', [
    [call(), call('delegate_vehicle_control', {'skill_ids': ['climate']}, 'b')],
    [call('delegate_vehicle_control', {'skill_ids': ['navigation']}),
     call('manage_navigation', {'action': 'start', 'place_ref': 'poi'}, 'b')],
    [call('delegate_vehicle_control', {'skill_ids': ['media']}),
     call('control_media', {'action': 'play', 'content_id': 'song'}, 'b')],
    [call(), call('set_navigation_destination', {'destination': '无来源机场'}, 'b')],
    [call('delegate_task', {'goal': '天气研究', 'query_domain': 'weather', 'query_mode': 'research'})],
    [call('manage_task', {'action': 'cancel', 'task_id': 'not-in-context'})],
])
async def test_semantic_rejection_exposes_no_part_of_a_group(calls):
    kernel = service(ScriptedModel([{'tool_calls': calls}]), semantic_policy=policy(set_navigation_destination=binding()))
    events = await collect(kernel.start(start_request()))
    assert events[-1].type == 'run.failed'
    assert not any(item.requests for item in events)


async def test_bindings_and_capabilities_must_both_authorize_the_route():
    request = start_request()
    request.context.capabilities.tools[0].available = False
    assert (await collect(service(ScriptedModel([{'tool_calls': [call()]}])).start(request)))[-1].type == 'run.failed'
    mismatched = policy(set_volume=binding('query', 'cloud'))
    assert (await collect(service(ScriptedModel([{'tool_calls': [call()]}]), semantic_policy=mismatched).start(start_request())))[-1].type == 'run.failed'


@pytest.mark.parametrize('source_turn,presented,allowed', [('older', True, True), ('turn-1', True, False), ('older', False, False)])
async def test_poi_requires_a_previously_presented_reference(source_turn, presented, allowed):
    request = advertise(start_request(), 'start_poi_navigation')
    request.context.cockpit = initial_cockpit_state()
    value = request.model_dump()
    value['context']['places'] = [{'place_ref': 'poi-1', 'name': '地点一', 'provider': 'fixture',
                                   'source_turn_id': source_turn, 'presented_to_user': presented}]
    kernel = service(ScriptedModel([{'tool_calls': [call('start_poi_navigation', {'place_id': 'poi-1'})]}]),
                     semantic_policy=policy(start_poi_navigation=binding()))
    event = (await collect(kernel.start(StartRequest.model_validate(value))))[-1]
    assert (event.type == 'run.suspended') is allowed


@pytest.mark.parametrize('kind,speed,known,allowed', [('music', 30, True, True), ('music', 0, False, False),
                                                  ('video', 30, True, False), ('video', 0, True, True)])
async def test_media_choice_provenance_and_driver_video_policy(kind, speed, known, allowed):
    request = advertise(start_request(), 'control_media', action='play')
    request.context.cockpit = initial_cockpit_state()
    value = request.model_dump()
    value['context']['vehicle']['state']['_meta']['speed_kph'] = speed
    value['context']['media'] = ([{'content_id': 'media-1', 'kind': kind, 'title': '真实目录条目', 'provider': 'fixture',
                                   'source_turn_id': 'older', 'presented_to_user': True}] if known else [])
    kernel = service(ScriptedModel([{'tool_calls': [call('control_media', {'action': 'play', 'content_id': 'media-1'})]}]),
                     semantic_policy=policy(**{'control_media.play': binding()}))
    assert ((await collect(kernel.start(StartRequest.model_validate(value))))[-1].type == 'run.suspended') is allowed


async def test_policy_cannot_mutate_canonical_request_while_returning_a_route():
    class MutatingPolicy:
        policy_id = 'offline-mutation-test'
        def prepare_product_action(self, *args):
            return binding()
        def validate_observations(self, *args):
            pass
        def validate_group(self, input_):
            input_.requests[0].arguments['volume'] = 99
            return [binding()]
    event = (await collect(service(ScriptedModel([{'tool_calls': [call()]}]), semantic_policy=MutatingPolicy()).start(start_request())))[-1]
    assert event.requests[0].request.arguments == {'volume': 40}
    assert event.checkpoint.run.pending[0].arguments == {'volume': 40}


async def test_same_run_media_search_receipt_grounds_playback_without_accepting_model_ids():
    request = advertise(start_request(), 'search_media', operation='query', location='cloud', content_kind='music')
    advertise(request, 'control_media', action='play')
    request.context.cockpit = initial_cockpit_state()
    model = ScriptedModel([{'tool_calls': [call('search_media', {'kind': 'music', 'query': '目录歌名'})]},
                           {'tool_calls': [call('control_media', {'action': 'play', 'content_id': 'verified-song'}, 'b')]},
                           {'content': '收到真实播放器回执'}])
    kernel = service(model, semantic_policy=policy(**{'search_media.music': binding('query', 'cloud'),
                                                     'control_media.play': binding()}))
    search = (await collect(kernel.start(request)))[-1]
    receipt = observation(search.checkpoint, ok=True, provider='fixture-catalog', items=[
        {'content_id': 'verified-song', 'kind': 'music', 'title': '目录歌名'}])
    playback = (await collect(kernel.resume(resume_request(search.checkpoint, [receipt]))))[-1]
    assert playback.type == 'run.suspended'
    assert playback.requests[0].request.arguments['content_id'] == 'verified-song'
    command = playback.requests[0].dispatch.prepared_command
    assert command['requires_content_resolution'] is True
    assert playback.requests[0].dispatch.operation == 'control'
    actual = {**command['expected'], 'version': command['expected_state_version'] + 1}
    final = (await collect(kernel.resume(resume_request(playback.checkpoint, [
        observation(playback.checkpoint, ok=True, executed=True, state=actual)]))))[-1]
    assert final.type == 'run.completed'


async def test_diagnostics_capture_model_input_handoff_observations_and_output_separately():
    class MemoryDiagnostics:
        def __init__(self):
            self.records = []
        async def emit(self, record):
            self.records.append(type(record).model_validate_json(record.model_dump_json()))
    sink = MemoryDiagnostics()
    kernel = service(ScriptedModel([{'tool_calls': [call('get_weather', {'location': '巴黎', 'date': 'today'})]},
                                   {'content': '答复'}]), diagnostics=sink)
    initial = (await collect(kernel.start(start_request())))[-1]
    final = await collect(kernel.resume(resume_request(initial.checkpoint, [observation(initial.checkpoint)])))
    events = [item.event for item in sink.records if item.event is not None]
    assert events[0].type == 'model.started'
    assert len(events[0].data['tools']) == 26
    assert any(item.type == 'run.suspended' and item.data['requests'] for item in events)
    assert any(item.kind == 'observations.received' and item.observations for item in sink.records)
    assert any(item.type == 'model.completed' and item.data['metadata'] == {'offline': True} for item in events)
    assert events[-1].type == 'run.completed'
    assert [item.type for item in final] == ['text.delta', 'run.completed']


async def test_diagnostic_sink_failure_never_exposes_a_tool_handoff():
    class FailedDiagnostics:
        async def emit(self, record):
            if record.event.type == 'run.suspended':
                raise OSError('offline trace persistence failure')
    model = ScriptedModel([{'tool_calls': [call()]}])
    run = service(model, diagnostics=FailedDiagnostics()).start(start_request())
    delivered = await collect(run)
    assert not any(item.requests for item in delivered)
    assert delivered[-1].type == 'run.failed'
    assert delivered[-1].error_code == 'diagnostics_unavailable'
    saved = GatewayCheckpoint.model_validate_json(delivered[-1].checkpoint.model_dump_json())
    assert saved.run.pending and saved.run.status == 'failed'
    with pytest.raises(RunProtocolError):
        await collect(run)
    with pytest.raises(RunProtocolError):
        await collect(service(model).resume(resume_request(saved)))


async def test_cancel_during_handoff_emits_terminal_event_if_consumer_keeps_reading():
    run = service(ScriptedModel([{'tool_calls': [call()]}])).start(start_request())
    outputs = []
    async for event in run.events():
        outputs.append(event)
        if event.type == 'run.suspended':
            run.cancel()
    assert [event.type for event in outputs] == ['run.suspended', 'run.cancelled']
    assert outputs[-1].checkpoint.run.pending
