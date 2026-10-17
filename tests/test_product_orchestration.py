"""Full product-policy boundaries exercised through the receipt-driven harness."""
import json

import httpx

from backend.cockpit import initial_cockpit_state
from backend.harness.background import TaskService
from backend.harness.gateway_contracts import TaskReference
from backend.harness.release import get_release_id
from backend.harness.task_transport import GatewayTaskStore
from test_orchestration import (GatewayLedgerFixture, StreamModel, call, kernel,
                                outputs, request, resume)


async def test_status_query_then_pause_uses_returned_state_and_waits_on_invalid_receipt():
    input_ = request('查询播放器再暂停', tools=[
        ('control_media', {'action': 'get_status'}, 'query', 'device'),
        ('control_media', {'action': 'pause'}, 'control', 'device')])
    input_.context.cockpit.pop('media')
    model = StreamModel({'tool_calls': [call('control_media', {'action': 'get_status'}),
                                       call('control_media', {'action': 'pause'}, 'b')]},
                        {'content': '播放器已返回状态。'})
    service = kernel(model, input_)
    first = (await outputs(service.start(input_)))[-1]
    assert first.type == 'run.suspended' and len(first.requests) == 1
    assert first.requests[0].request.arguments == {'action': 'get_status'}
    invalid = (await outputs(resume(service, first, {'ok': True, 'state': {'status': 'playing'}})))[-1]
    assert invalid.checkpoint.run.status == 'waiting_reconciliation' and not invalid.requests
    actual = {**initial_cockpit_state()['media'], 'version': 7, 'content_id': 'cp:live',
              'kind': 'music', 'title': '实际播放项目', 'provider': 'cp',
              'status': 'playing', 'desired_status': 'playing', 'position_seconds': 12}
    second = (await outputs(resume(service, invalid, {'ok': True, 'state': actual})))[-1]
    assert len(second.requests) == 1 and second.requests[0].request.arguments == {'action': 'pause'}
    command = second.requests[0].dispatch.prepared_command
    assert command['expected_state_version'] == 7
    assert command['expected']['content_id'] == 'cp:live' and command['expected']['position_seconds'] == 12
    final = (await outputs(resume(service, second, {'ok': True, 'executed': True,
        'state': {**command['expected'], 'version': 8}})))[-1]
    assert final.type == 'run.completed' and len(model.inputs) == 2


async def test_two_task_updates_in_one_group_bind_each_actual_revision():
    ledger = GatewayLedgerFixture()
    async with httpx.AsyncClient(transport=httpx.MockTransport(ledger.http)) as client:
        store = GatewayTaskStore('https://gateway.test', client=client)
        seed = await store.for_scope('conversation').create_task('task', '核实资料',
            {'release_id': get_release_id(), 'model_policy_id': 'offline-policy'}, 'seed')
        input_ = request('把条件更新两次')
        input_.context.tasks = [TaskReference.model_validate({key: seed[key]
            for key in ('id', 'revision', 'status', 'goal', 'context')})]
        model = StreamModel({'tool_calls': [
            call('manage_task', {'action': 'update', 'task_id': 'task', 'text': '限定本年'}, 'a'),
            call('manage_task', {'action': 'update', 'task_id': 'task', 'text': '仅公开资料'}, 'b')]})
        service = kernel(model, input_, task_service=TaskService(store, lambda bound: None,
                                                               model_policy_id='offline-policy'))
        result = (await outputs(service.start(input_)))[-1]
        assert result.type == 'run.completed' and not result.requests
        assert ledger.tasks['task']['revision'] == 3
        assert ledger.tasks['task']['constraints'] == ['限定本年', '仅公开资料']
        writes = [body for path, body in ledger.requests if path.endswith('/mutate')]
        assert [body['expected_revision'] for body in writes] == [1, 2]
        assert writes[0]['idempotency_key'] != writes[1]['idempotency_key']
        assert len(model.inputs) == 1


async def test_task_created_earlier_in_run_can_be_selected_from_actual_registration():
    ledger = GatewayLedgerFixture()
    async with httpx.AsyncClient(transport=httpx.MockTransport(ledger.http)) as client:
        store = GatewayTaskStore('https://gateway.test', client=client)
        input_ = request('核实公开信息并查天气', tools=[('get_weather', {}, 'query', 'cloud')])
        model = StreamModel({'tool_calls': [call('delegate_task', {'goal': '核实公开信息'}, 'task'),
                                           call('get_weather', {'location': '巴黎', 'date': 'today'}, 'weather')]},
                            {'tool_calls': [call('manage_task', {'action': 'update', 'text': '仅公开资料'}, 'update')]})
        service = kernel(model, input_, task_service=TaskService(store, lambda bound: None,
                                                               model_policy_id='offline-policy'))
        first = (await outputs(service.start(input_)))[-1]
        assert first.type == 'run.suspended' and first.requests[0].request.name == 'get_weather'
        assert input_.context.tasks == [] and len(ledger.tasks) == 1
        final = (await outputs(resume(service, first, {'ok': True, 'forecast': []})))[-1]
        assert final.type == 'run.completed'
        task = next(iter(ledger.tasks.values()))
        assert task['revision'] == 2 and task['constraints'] == ['仅公开资料']



async def test_actual_status_queue_survives_model_compaction_and_drives_later_next():
    input_ = request('查询后播放下一项', tools=[
        ('control_media', {'action': 'get_status'}, 'query', 'device'),
        ('control_media', {'action': 'next'}, 'control', 'device')])
    input_.context.cockpit.pop('media')
    model = StreamModel({'tool_calls': [call('control_media', {'action': 'get_status'})]},
                        {'tool_calls': [call('control_media', {'action': 'next'}, 'next')]})
    service = kernel(model, input_)
    query = (await outputs(service.start(input_)))[-1]
    queue = [{'content_id': 'cp:' + str(index), 'kind': 'music', 'title': '歌曲' + str(index),
              'provider': 'cp', 'album': '实际目录元信息' * 60} for index in range(10)]
    actual = {**initial_cockpit_state()['media'], **queue[8], 'version': 4,
              'status': 'playing', 'desired_status': 'playing', 'queue': queue}
    next_ = (await outputs(resume(service, query, {'ok': True, 'state': actual})))[-1]
    assert next_.type == 'run.suspended'
    command = next_.requests[0].dispatch.prepared_command
    assert command['expected']['content_id'] == 'cp:9' and command['expected_state_version'] == 4
    assert len(command['expected']['queue']) == 10
    assert len(next_.checkpoint.run.messages[-2]['content']) > 5000
    visible = json.loads(model.inputs[1][-1]['content'])
    assert visible['state']['queue'][9]['content_id'] == 'cp:9'
    assert visible['state']['version'] == 4
    final = (await outputs(resume(service, next_, {'ok': True, 'executed': True,
        'state': {**command['expected'], 'version': 5}})))[-1]
    assert final.type == 'run.completed'
