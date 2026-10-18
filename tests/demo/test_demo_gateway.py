"""Local integration adapters preserve the production handoff invariants."""
from copy import deepcopy

import pytest

from super_xd.demo.tasks.database import Database
from super_xd.demo.gateway import LocalGateway
from super_xd.demo.tasks.ledger import SQLiteGatewayTaskStore
from super_xd.nlp.contracts.run import ToolObservation
from tests.support.gateway_contract import ScriptedModel, call, service, start_request


async def test_driver_commits_complete_handoff_before_any_execution():
    model = ScriptedModel([{'tool_calls': [call(), call('get_weather', {'location': '巴黎', 'date': 'today'}, 'weather')]},
                           {'content': '巴黎天气查询结果已返回。'}])
    start, executed = start_request(), []
    gateway = None

    async def execute(item, request):
        assert gateway.commit is not None
        saved = gateway.commit
        assert saved.checkpoint.start.identity == request.identity
        assert item.request.invocation_id in {row.request.invocation_id for row in saved.requests}
        assert len(saved.requests) == 2
        executed.append(item.request.invocation_id)
        state = deepcopy(start.context.vehicle.state)
        state['media']['volume'] = 40
        result = ({'ok': True, 'executed': True, 'vehicle_state': state} if item.request.name == 'set_volume'
                  else {'ok': True, 'summary': {'temperature_c': 18}})
        return ToolObservation(identity=start.identity, invocation_id=item.request.invocation_id, status='completed', result=result)

    gateway = LocalGateway(service(model), start, execute)
    events = [event async for event in gateway.events()]
    assert events[-1].type == 'run.completed'
    assert len(executed) == len(set(executed)) == 2
    assert len(model.requests) == 2


async def test_unknown_device_outcome_is_saved_and_never_redispatched():
    model, calls = ScriptedModel([{'tool_calls': [call()]}]), []
    start = start_request()

    async def execute(item, request):
        calls.append(item.request.invocation_id)
        return ToolObservation(identity=request.identity, invocation_id=item.request.invocation_id,
                               status='unknown', result={'ok': False, 'outcome_unknown': True})

    gateway = LocalGateway(service(model), start, execute)
    events = [event async for event in gateway.events()]
    assert events[-1].type == 'run.suspended' and events[-1].requests == []
    assert len(calls) == 1 and len(model.requests) == 1
    assert gateway.observations[calls[0]].status == 'unknown'
    assert gateway.commit.checkpoint.run.status == 'waiting_reconciliation'


async def test_sqlite_task_commands_are_idempotent_scoped_and_atomic(tmp_path):
    db = Database(tmp_path / 'ledger.sqlite3')
    store = SQLiteGatewayTaskStore(db).for_scope('conversation')
    original = await store.create_task('task', '查证公开信息', {'release_id': 'r1'}, 'register-1')
    duplicate = await store.create_task('task', '查证公开信息', {'release_id': 'r1'}, 'register-1')
    assert original == duplicate and len(db.list_tasks('conversation')) == 1
    events = db.events_after(0, 'conversation')
    with pytest.raises(ValueError):
        await store.create_task('task', '更改内容', {}, 'register-1')
    assert db.events_after(0, 'conversation') == events
    changed = await store.mutate_task('task', 1, 'update', '补充范围', 'update-1')
    repeated = await store.mutate_task('task', 1, 'update', '补充范围', 'update-1')
    assert changed == repeated and changed['revision'] == 2 and changed['status'] == 'queued'
    assert len(db.events_after(0, 'conversation')) == len(events) + 1
    foreign = SQLiteGatewayTaskStore(db).for_scope('other')
    assert await foreign.read('task') is None
    assert await foreign.save('task', 2, summary='越界') is False
    with pytest.raises(ValueError):
        await foreign.mutate_task('task', 2, 'cancel', '', 'other-cancel')
    assert db.get_task('task') == changed
