"""Delivery changes never restart the durable research execution."""
from copy import deepcopy

import pytest

from super_xd.demo.tasks.database import Database


@pytest.mark.parametrize('query_mode', ['quick', 'research'])
def test_delivery_transition_preserves_execution_checkpoint_and_budgets(tmp_path, query_mode):
    db = Database(tmp_path / 'tasks.sqlite')
    task = db.create_task('conversation', '比较公开资料', {'query_mode': query_mode}, delivery_mode='foreground')
    running = db.claim_next()
    checkpoint = {'messages': [{'role': 'tool', 'content': '真实已读取材料'}], 'tool_count': 2}
    assert db.save_task(task['id'], 1, steps=3, checkpoint=checkpoint)
    before = db.get_task(task['id'])
    changed = db.continue_in_background(task['id'], 1, 'soft-deadline')
    assert changed['id'] == task['id'] and changed['revision'] == 1
    assert changed['delivery_mode'] == 'background'
    for key in ('status', 'steps', 'checkpoint', 'running_seconds', 'running_since', 'deadline_at', 'context'):
        assert changed[key] == before[key]
    assert running['status'] == changed['status'] == 'running'
    assert len(db.list_tasks('conversation')) == 1
    repeated = db.continue_in_background(task['id'], 1, 'soft-deadline')
    assert repeated == changed
    rows = [row for row in db.events_after(0, session_id='conversation') if row['kind'] == 'delivery_changed']
    assert len(rows) == 1 and rows[0]['payload']['task']['delivery_mode'] == 'background'


@pytest.mark.parametrize('action', ['update', 'cancel'])
def test_stale_delivery_transition_cannot_overwrite_user_revision(tmp_path, action):
    db = Database(tmp_path / 'tasks.sqlite')
    task = db.create_task('conversation', '核实资料', delivery_mode='foreground')
    updated = db.mutate_task(task['id'], action, '采用新范围' if action == 'update' else '', 1)
    before = deepcopy(updated)
    with pytest.raises(ValueError, match='revision'):
        db.continue_in_background(task['id'], 1, 'stale-deadline')
    assert db.get_task(task['id']) == before


def test_disconnected_foreground_can_detach_a_just_completed_result(tmp_path):
    db = Database(tmp_path / 'tasks.sqlite')
    task = db.create_task('conversation', '核实资料', delivery_mode='foreground')
    db.save_task(task['id'], 1, status='completed', result={'summary': '已读取材料'})
    changed = db.continue_in_background(task['id'], 1, 'completion-race')
    assert changed['status'] == 'completed' and changed['delivery_mode'] == 'background'
    assert changed['result'] == {'summary': '已读取材料'}
    assert len([row for row in db.events_after(0, session_id='conversation') if row['kind'] == 'delivery_changed']) == 1


@pytest.fixture
def foreground_service(tmp_path):
    from super_xd.demo.tasks.ledger import SQLiteGatewayTaskStore
    from super_xd.nlp.orchestration.tasks.service import TaskService
    db = Database(tmp_path / 'service.sqlite')
    store = SQLiteGatewayTaskStore(db)
    service = TaskService(store, lambda unused: pytest.fail('waiter must not start another runner'),
                          model_policy_id='models', release_id='release')
    task = db.create_task('conversation', '比较公开来源',
                          {'release_id': 'release', 'model_policy_id': 'models', 'query_mode': 'quick'},
                          delivery_mode='foreground')
    return db, service, task


async def test_foreground_soft_deadline_changes_only_delivery(foreground_service):
    db, service, task = foreground_service
    current = db.claim_next()
    result = await service.await_result('conversation', task['id'], 1, timeout_seconds=.01,
                                         idempotency_key='detach')
    assert result['delivery_mode'] == 'background'
    assert result['status'] == 'running'
    assert result['running_since'] == current['running_since']
    assert result['steps'] == 0 and len(db.list_tasks('conversation')) == 1


async def test_foreground_returns_completed_result_without_detaching(foreground_service):
    db, service, task = foreground_service
    db.save_task(task['id'], 1, status='completed', result={'summary': '实证结果'})
    result = await service.await_result('conversation', task['id'], 1, timeout_seconds=.01,
                                         idempotency_key='unused')
    assert result['result']['summary'] == '实证结果'
    assert result['delivery_mode'] == 'foreground'


async def test_waiter_cancellation_preserves_research_for_background(foreground_service):
    import asyncio
    db, service, task = foreground_service
    waiter = asyncio.create_task(service.await_result('conversation', task['id'], 1,
                                  timeout_seconds=5, idempotency_key='disconnect'))
    await asyncio.sleep(.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    current = db.get_task(task['id'])
    assert current['delivery_mode'] == 'background'
    assert current['status'] == 'queued' and current['revision'] == 1


async def test_foreground_cannot_return_result_from_user_superseding_revision(foreground_service):
    from super_xd.nlp.engine.run import RunProtocolError
    db, service, task = foreground_service
    db.mutate_task(task['id'], 'update', '只比较本年', 1)
    with pytest.raises(RunProtocolError, match='revision'):
        await service.await_result('conversation', task['id'], 1, timeout_seconds=.01,
                                    idempotency_key='stale')
    assert db.get_task(task['id'])['revision'] == 2


async def test_cancelling_waiter_cannot_hang_on_unreachable_delivery_store(foreground_service, monkeypatch):
    import asyncio
    db, service, task = foreground_service
    service.foreground_wait_seconds = .02
    attempted = []
    async def unreachable(request):
        attempted.append(request)
        await asyncio.Event().wait()
    monkeypatch.setattr(service, 'continue_in_background', unreachable)
    waiter = asyncio.create_task(service.await_result('conversation', task['id'], 1,
                                  timeout_seconds=5, idempotency_key='disconnect-uncertain'))
    await asyncio.sleep(.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter, .2)
    assert len(attempted) == 1
    assert db.get_task(task['id'])['status'] == 'queued'
