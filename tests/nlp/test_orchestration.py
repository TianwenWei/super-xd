"""End-to-end NLP composition with explicit model/device/HTTP-ledger fixtures."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from super_xd.demo.devices.simulator import execute_tool, initial_vehicle_state
from super_xd.demo.devices.cockpit import initial_cockpit_state
from super_xd.nlp.contracts.tasks import TaskExecutionRequest, TaskMutationRequest, TaskPresentationRequest, TaskPresentationResult
from super_xd.nlp.orchestration.tasks.service import TaskService
from super_xd.nlp.orchestration.gateway import NLPService
from super_xd.nlp.contracts.gateway_v1 import GatewayOutput, ResumeRequest, StartRequest, TaskReference
from super_xd.nlp.contracts.run import ToolObservation
from super_xd.nlp.policy.product import ProductTalkerPolicy
from super_xd.nlp.release import get_release_id
from super_xd.nlp.engine.run import RunProtocolError
from super_xd.nlp.orchestration.tasks.runner import TaskRunner
from super_xd.nlp.adapters.gateway.tasks import GatewayConflict, GatewayTaskStore
from super_xd.nlp.orchestration.vehicle_planner import VehicleControlPlanner


from tests.support.orchestration import (
    call, message, StreamModel, CompleteModel, request, product_policy, kernel, outputs, resume, planner_submission, planned_fixture, GatewayLedgerFixture, SearchFixture, execution
)


async def test_planner_is_internal_and_actions_advance_only_on_true_receipts():
    input_, talker, planner, service = planned_fixture()
    first = (await outputs(service.start(input_)))[-1]
    assert first.type == 'run.suspended'
    assert [item.request.name for item in first.requests] == ['set_mirror_heating']
    assert first.requests[0].dispatch.kind == 'tool_execution'
    assert first.checkpoint.orchestration.model_calls == 1
    assert json.loads(planner.inputs[0][2]['content'])['capabilities'] == {'gps': True, 'real_navigation': True}
    unknown = (await outputs(resume(service, first, {'reason': 'receipt delayed'}, status='unknown')))[-1]
    assert unknown.checkpoint.run.status == 'waiting_reconciliation' and not unknown.requests
    assert len(planner.inputs) == 1 and len(talker.inputs) == 1
    vehicle = deepcopy(input_.context.vehicle.state)
    execute_tool('set_mirror_heating', {'enabled': True}, vehicle)
    second = (await outputs(resume(service, unknown, {'ok': True, 'executed': True, 'vehicle_state': vehicle})))[-1]
    assert [item.request.name for item in second.requests] == ['set_mirror_fold']
    assert second.requests[0].request.idempotency_key != first.requests[0].request.idempotency_key
    execute_tool('set_mirror_fold', {'folded': True}, vehicle)
    complete = (await outputs(resume(service, second, {'ok': True, 'executed': True, 'vehicle_state': vehicle})))[-1]
    assert complete.type == 'run.completed'
    assert len(talker.inputs) == len(planner.inputs) == 1
    group = complete.checkpoint.orchestration.groups[-1]
    assert len(group.completed_plans[0].receipts) == 2
    assert group.completed_plans[0].plan.loaded_skills == ['mirrors']
    assert complete.checkpoint.orchestration.tool_calls == 2


async def test_planner_cancel_never_releases_second_action_or_revives_old_run():
    input_, _, _, service = planned_fixture()
    first = (await outputs(service.start(input_)))[-1]
    cancelled = (await outputs(resume(service, first, cancel=True)))[-1]
    assert cancelled.type == 'run.cancelled' and not cancelled.requests
    with pytest.raises(RunProtocolError):
        await outputs(resume(service, cancelled, {'ok': True, 'executed': True}))


async def test_cancel_interrupts_planner_model_before_any_device_handoff():
    input_ = request()
    entered, closed = asyncio.Event(), asyncio.Event()
    async def blocked():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            closed.set()
    service = kernel(StreamModel(message('delegate_vehicle_control', {'skill_ids': ['mirrors']})), input_,
                     planner=VehicleControlPlanner(CompleteModel(blocked)))
    run = service.start(input_)
    job = asyncio.create_task(outputs(run))
    await entered.wait()
    run.cancel()
    events = await asyncio.wait_for(job, 1)
    assert closed.is_set() and events[-1].type == 'run.cancelled'
    assert not any(item.requests for item in events)


async def test_control_precheck_cannot_be_mistaken_for_execution():
    input_ = request('音量40', tools=[('set_volume', {}, 'control', 'device')])
    model = StreamModel(message('set_volume', {'volume': 40}))
    service = kernel(model, input_)
    first = (await outputs(service.start(input_)))[-1]
    waiting = (await outputs(resume(service, first, {'ok': True, 'executed': False, 'simulated': True})))[-1]
    assert waiting.type == 'run.suspended' and waiting.checkpoint.run.status == 'waiting_reconciliation'
    assert not waiting.requests and len(model.inputs) == 1
    actual = {**first.requests[0].dispatch.prepared_command['expected'], 'version': 1}
    done = (await outputs(resume(service, waiting, {'ok': True, 'executed': True, 'state': actual})))[-1]
    assert done.type == 'run.completed' and len(model.inputs) == 1


async def test_last_planner_write_requires_actual_snapshot_before_completion():
    input_, talker, planner, service = planned_fixture()
    first = (await outputs(service.start(input_)))[-1]
    vehicle = deepcopy(input_.context.vehicle.state)
    execute_tool('set_mirror_heating', {'enabled': True}, vehicle)
    second = (await outputs(resume(service, first, {'ok': True, 'executed': True, 'vehicle_state': vehicle})))[-1]
    waiting = (await outputs(resume(service, second, {'ok': True, 'executed': True})))[-1]
    assert waiting.type == 'run.suspended' and waiting.checkpoint.run.status == 'waiting_reconciliation'
    assert not waiting.requests
    assert {**input_.context.vehicle.state, **waiting.checkpoint.orchestration.groups[-1].vehicle_state} == vehicle
    execute_tool('set_mirror_fold', {'folded': True}, vehicle)
    done = (await outputs(resume(service, waiting, {'ok': True, 'executed': True, 'vehicle_state': vehicle})))[-1]
    assert done.type == 'run.completed'
    parent_result = next(iter(done.checkpoint.orchestration.groups[-1].results.values()))
    assert parent_result.result['vehicle_state'] == vehicle
    assert len(talker.inputs) == len(planner.inputs) == 1


async def test_media_play_pause_group_prepares_second_action_from_actual_version():
    input_ = request('播放后暂停', tools=[('control_media', {'action': 'play'}, 'control', 'device'),
                                        ('control_media', {'action': 'pause'}, 'control', 'device')])
    value = input_.model_dump()
    value['context']['media'] = [{'content_id': 'catalog:a', 'kind': 'music', 'title': '目录音乐',
        'provider': 'catalog', 'source_turn_id': 'older', 'presented_to_user': True}]
    input_ = StartRequest.model_validate(value)
    model = StreamModel({'tool_calls': [call('control_media', {'action': 'play', 'content_id': 'catalog:a'}),
                                       call('control_media', {'action': 'pause'}, 'b')]})
    service = kernel(model, input_)
    first = (await outputs(service.start(input_)))[-1]
    assert len(first.requests) == 1
    command = first.requests[0].dispatch.prepared_command
    assert command['expected_state_version'] == 0
    actual = {**command['expected'], 'version': 1}
    second = (await outputs(resume(service, first, {'ok': True, 'executed': True, 'state': actual})))[-1]
    assert len(second.requests) == 1 and second.requests[0].request.arguments == {'action': 'pause'}
    command = second.requests[0].dispatch.prepared_command
    assert command['expected_state_version'] == 1 and command['expected']['status'] == 'paused'
    final = (await outputs(resume(service, second, {'ok': True, 'executed': True, 'state': {**command['expected'], 'version': 2}})))[-1]
    assert final.type == 'run.completed' and len(model.inputs) == 1


async def test_background_registration_http_execution_clarification_reply_and_completion():
    ledger = GatewayLedgerFixture()
    async with httpx.AsyncClient(transport=httpx.MockTransport(ledger.http)) as client:
        store = GatewayTaskStore('https://gateway.test', client=client, secrets=('fixture-secret',))
        thinker = CompleteModel(message('ask_clarification', {'question': '需要核实哪个时间范围？'}),
            message('search_web', {'query': '公开信息'}), message('extract_web', {'urls': ['https://example.org/source']}),
            {**message('finish_task', {'summary': '已从真实测试资料核实结果。'}), '_meta': {'api_key': 'fixture-secret'}})
        tasks = TaskService(store, lambda bound: TaskRunner(bound, thinker, SearchFixture(), trace_sink=bound), model_policy_id='offline-policy')
        input_ = request('帮我核实公开信息')
        service = kernel(StreamModel(message('delegate_task', {'goal': '核实公开信息'})), input_, task_service=tasks)
        registered = (await outputs(service.start(input_)))[-1]
        assert registered.type == 'run.completed' and not registered.requests
        task = next(iter(ledger.tasks.values()))
        assert task['status'] == 'queued' and task['context']['release_id'] == get_release_id()
        first = await service.execute_task(execution(task))
        assert first.task['status'] == 'waiting_clarification' and first.task['checkpoint']['messages']
        clarification = await service.present_task(TaskPresentationRequest(conversation_id='conversation', task_id=task['id'],
            revision=1, expected_status='waiting_clarification', persona_id='warm'))
        assert clarification.kind == 'clarify' and '时间范围' in clarification.speech
        saved_step = first.task['steps']
        changed = await service.mutate_task(TaskMutationRequest(conversation_id='conversation', task_id=task['id'],
            expected_revision=1, action='reply', text='核实今年的资料', idempotency_key='reply-1'))
        assert changed['revision'] == 2
        stale_speech = await service.present_task(TaskPresentationRequest(conversation_id='conversation', task_id=task['id'],
            revision=1, expected_status='waiting_clarification'))
        assert stale_speech.stale and stale_speech.speech == '' and stale_speech.card is None
        stale = await service.execute_task(execution(task, revision=1))
        assert stale.stale and len(thinker.inputs) == 1
        done = await service.execute_task(execution(task))
        assert done.task['status'] == 'completed' and done.task['steps'] > saved_step
        assert done.task['result']['web_evidence'] == 'extracted'
        assert any('今年' in str(message_) for message_ in thinker.inputs[-1])
        presented = await service.present_task(TaskPresentationRequest(conversation_id='conversation', task_id=task['id'],
            revision=2, expected_status='completed'))
        presented = TaskPresentationResult.model_validate_json(presented.model_dump_json())
        assert presented.kind == 'result' and '真实测试资料' in presented.speech
        assert presented.card['suggestions_require_new_run'] is True
        assert len(thinker.inputs) == 4
        assert not any('fixture-secret' in json.dumps(record) or 'valid-lease' in json.dumps(record) for record in ledger.traces)
        assert any(body.get('lease_token') == 'valid-lease' for path, body in ledger.requests if path.endswith('/save'))
        with pytest.raises(GatewayConflict):
            await service.execute_task(execution(task, lease_token='expired'))
        with pytest.raises(RunProtocolError):
            await service.execute_task(execution(task, model_policy_id='changed-model'))


async def test_background_unknown_commit_is_explicit_and_never_retried():
    calls = []
    async def unavailable(request):
        calls.append(request)
        raise httpx.ReadTimeout('external commit outcome unavailable')
    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        tasks = TaskService(GatewayTaskStore('https://gateway.test', client=client), lambda bound: None, model_policy_id='offline-policy')
        input_ = request()
        service = kernel(StreamModel(message('delegate_task', {'goal': '核实信息'})), input_, task_service=tasks)
        failed = (await outputs(service.start(input_)))[-1]
        assert failed.type == 'run.failed' and failed.error_code == 'task_commit_unknown'
        assert failed.checkpoint.orchestration.groups[-1].task_commit.status == 'unknown'
        assert failed.checkpoint.orchestration.groups[-1].task_commit.task_id.startswith('task-')
        assert len(calls) == 1


async def test_task_query_compacts_only_model_request_and_preserves_raw_receipt():
    ledger = GatewayLedgerFixture()
    class Diagnostics:
        def __init__(self):
            self.records = []
        async def emit(self, record):
            self.records.append(record.model_dump())
    diagnostics = Diagnostics()
    async with httpx.AsyncClient(transport=httpx.MockTransport(ledger.http)) as client:
        store = GatewayTaskStore('https://gateway.test', client=client)
        seed = await store.for_scope('conversation').create_task('task', '核实公开资料',
            {'release_id': get_release_id(), 'model_policy_id': 'offline-policy'}, 'seed')
        seed = ledger.tasks['task']
        seed.update(status='completed', constraints=['仅官方原始资料'], result={'summary': '已核实事实', 'spoken_summary': '已核实事实'},
                    checkpoint={'checkpoint_version': 1, 'release_id': get_release_id(), 'goal': seed['goal'],
                        'revision': seed['revision'], 'messages': [{'role': 'system', 'content': 'private-thinker-archive'}],
                        'sources': [], 'suggestions': [], 'observations': [], 'tool_count': 0,
                        'retrieval': {'version': 1, 'records': []}, 'constraint_count': 1},
                    transcript=[{'diagnostic': 'private-thinker-trace'}])
        input_ = request('查看核实任务结果')
        input_.context.tasks = [TaskReference.model_validate({key: seed[key] for key in ('id', 'revision', 'status', 'goal', 'context')})]
        model = StreamModel(message('manage_task', {'action': 'get', 'task_id': 'task'}), {'content': '已核实事实。'})
        service = kernel(model, input_, task_service=TaskService(store, lambda bound: None, model_policy_id='offline-policy'),
                         diagnostics=diagnostics)
        done = (await outputs(service.start(input_)))[-1]
        assert done.type == 'run.completed' and len(model.inputs) == 2
        actual = model.inputs[-1]
        assert 'private-thinker-archive' not in json.dumps(actual)
        assert 'private-thinker-trace' not in json.dumps(actual)
        tool = json.loads(next(record['content'] for record in reversed(actual) if record['role'] == 'tool'))
        assert tool['task']['constraints'] == ['仅官方原始资料'] and tool['task']['result']['spoken_summary'] == '已核实事实'
        archived = json.loads(next(record['content'] for record in reversed(done.checkpoint.run.messages) if record['role'] == 'tool'))
        assert archived['task']['checkpoint']['messages'][0]['content'] == 'private-thinker-archive'
        starts = [record['event']['data']['messages'] for record in diagnostics.records
                  if record.get('event') and record['event']['type'] == 'model.started']
        assert starts[-1] == actual


@pytest.mark.parametrize('invalid', [{'revision': 1}, {'status': 'completed'}])
async def test_task_mutation_requires_exact_committed_revision_and_status(invalid):
    ledger = GatewayLedgerFixture()
    async def http(request_):
        response = await ledger.http(request_)
        if request_.url.path.endswith('/mutate'):
            body = response.json()
            body['task'].update(invalid)
            return httpx.Response(200, json=body)
        return response
    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
        store = GatewayTaskStore('https://gateway.test', client=client)
        await store.for_scope('conversation').create_task('task', '核实资料',
            {'release_id': get_release_id(), 'model_policy_id': 'offline-policy'}, 'seed')
        service = TaskService(store, lambda bound: None, model_policy_id='offline-policy')
        with pytest.raises(RunProtocolError, match='committed revision/status'):
            await service.mutate(TaskMutationRequest(conversation_id='conversation', task_id='task', expected_revision=1,
                action='update', text='仅官方资料', idempotency_key='update'))


async def test_waiting_confirmation_cannot_execute_or_be_presented_as_a_result():
    ledger = GatewayLedgerFixture()
    async with httpx.AsyncClient(transport=httpx.MockTransport(ledger.http)) as client:
        store = GatewayTaskStore('https://gateway.test', client=client)
        await store.for_scope('conversation').create_task('task', '核实资料',
            {'release_id': get_release_id(), 'model_policy_id': 'offline-policy'}, 'seed')
        ledger.tasks['task']['status'] = 'awaiting_confirm'
        def no_runner(bound):
            raise AssertionError('Awaiting confirmation cannot start reasoning')
        service = TaskService(store, no_runner, model_policy_id='offline-policy')
        result = await service.execute(execution(ledger.tasks['task']))
        assert result.task['status'] == 'awaiting_confirm'
        with pytest.raises(RunProtocolError, match='confirmation contract'):
            await service.present(TaskPresentationRequest(conversation_id='conversation', task_id='task', revision=1,
                                                         expected_status='awaiting_confirm'))


async def test_planned_keep_constraint_is_handed_to_executor_and_checked_against_actual_state():
    input_ = request(text='加热再折叠后视镜，音量保持不变', tools=[
        ('set_mirror_heating', {}, 'control', 'device'), ('set_mirror_fold', {}, 'control', 'device')])
    proposed = planner_submission(input_.user_text)
    body = json.loads(proposed['tool_calls'][0]['function']['arguments'])
    body['intents'][0]['source_text'] = '加热再折叠后视镜'
    body['intents'].append({'id': 'keep', 'source_text': '音量保持不变', 'status': 'keep',
        'action_ids': [], 'completed_operation_ids': [], 'keep_paths': ['media.volume'], 'question': '', 'reason': ''})
    body['keep_paths'] = ['media.volume']
    talker = StreamModel({'tool_calls': [call('delegate_vehicle_control', {'skill_ids': ['mirrors']})]},
                         {'content': '设备回执显示音量发生变化，已停止后续折叠操作。'})
    service = kernel(talker, input_, planner=VehicleControlPlanner(CompleteModel(message('submit_vehicle_plan', body))))
    first = (await outputs(service.start(input_)))[-1]
    assert first.requests[0].dispatch.execution_constraints == {
        'keep_baseline': {'media.volume': input_.context.vehicle.state['media']['volume']}}
    actual = deepcopy(input_.context.vehicle.state)
    actual['mirrors']['heating'] = True
    actual['media']['volume'] += 1
    done = (await outputs(resume(service, first, {'ok': True, 'executed': True, 'vehicle_state': actual})))[-1]
    assert done.type == 'run.completed' and not done.requests
    result = next(iter(done.checkpoint.orchestration.groups[0].results.values())).result
    assert result['code'] == 'keep_constraint_violated' and result['executed'] is True
    assert result['vehicle_state'] == actual and result['remaining_actions_not_executed'] is True


async def test_pending_planner_action_cannot_change_during_restore():
    input_, talker, planner, service = planned_fixture()
    first = (await outputs(service.start(input_)))[-1]
    checkpoint = first.checkpoint.model_copy(deep=True)
    checkpoint.orchestration.groups[-1].external_pending[0].request.arguments['enabled'] = False
    with pytest.raises(RunProtocolError, match='differs from the accepted plan'):
        service.resume(ResumeRequest(identity=input_.identity, checkpoint=checkpoint))


async def test_followup_planner_cannot_reissue_an_accepted_hot_operation():
    start = request(text='音量设为40，然后开启后视镜加热', tools=[
        ('set_volume', {}, 'control', 'device'), ('set_mirror_heating', {}, 'control', 'device')])
    class RepeatingPlanner:
        async def plan(self, text, vehicle, skills, **options):
            self.operations = options['completed_operations']
            return {'status': 'planned', 'ok': True, 'executed': False, 'loaded_skills': ['mirrors'],
                'intents': [{'id': 'whole', 'source_text': text, 'source_start': 0, 'source_end': len(text),
                    'status': 'planned', 'action_ids': ['repeat', 'heat'], 'keep_paths': [], 'question': '', 'reason': ''}],
                'actions': [{'id': 'repeat', 'intent_id': 'whole', 'name': 'set_volume',
                    'arguments': {'volume': 40}, 'depends_on': []},
                    {'id': 'heat', 'intent_id': 'whole', 'name': 'set_mirror_heating',
                    'arguments': {'enabled': True}, 'depends_on': ['repeat']}],
                'keep_paths': [], 'question': '', 'reason': ''}
    planner = RepeatingPlanner()
    model = StreamModel({'tool_calls': [call('set_volume', {'volume': 40}, 'volume')]},
                        {'tool_calls': [call('delegate_vehicle_control', {'skill_ids': ['mirrors']}, 'remaining')]},
                        {'content': '音量已经设为40；后续计划重复了该动作，没有继续执行。'})
    service = kernel(model, start, planner=planner, receipt_fast_path=False)
    first = (await outputs(service.start(start)))[-1]
    actual = deepcopy(start.context.vehicle.state)
    execute_tool('set_volume', {'volume': 40}, actual)
    cockpit_actual = deepcopy(start.context.cockpit['media'])
    cockpit_actual.update(volume=40, version=1)
    done = (await outputs(resume(service, first, {'ok': True, 'executed': True, 'vehicle_state': actual,
        'state': cockpit_actual, 'state_version': 1})))[-1]
    assert done.type == 'run.completed' and not done.requests
    assert len(planner.operations) == 1 and planner.operations[0]['arguments'] == {'volume': 40}
    rejected = next(iter(done.checkpoint.orchestration.groups[-1].results.values())).result
    assert rejected['code'] == 'vehicle_plan_invalid' and rejected['reason'] == 'already_completed_action'
    assert rejected['executed'] is False and rejected['receipts'] == []


async def test_cancelled_child_partial_is_diagnostic_only_and_redacted():
    entered = asyncio.Event()
    class Planner:
        async def plan(self, *args, **options):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                error.trace_partial = {'late_return': {'content': '过期模型内容', 'token': 'secret-fixture'}}
                raise
    class Diagnostics:
        def __init__(self): self.records = []
        async def emit(self, record): self.records.append(record)
    start = request(tools=[('set_mirror_heating', {}, 'control', 'device')])
    diagnostics = Diagnostics()
    service = kernel(StreamModel({'tool_calls': [call('delegate_vehicle_control', {'skill_ids': ['mirrors']})]}),
                     start, planner=Planner(), diagnostics=diagnostics)
    run = service.start(start)
    pending = asyncio.create_task(outputs(run))
    await entered.wait()
    run.cancel()
    delivered = await asyncio.wait_for(pending, 1)
    assert delivered[-1].type == 'run.cancelled' and not any(item.requests or item.text for item in delivered)
    event = next(record.event for record in diagnostics.records if record.event and record.event.type == 'run.cancelled')
    assert event.data['partial']['late_return'] == {'content': '过期模型内容', 'token': '[redacted]'}
