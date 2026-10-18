"""Registration certainty survives later foreground delivery failures."""
from evals.gateway_driver import MemoryTaskLedger
from super_xd.nlp.adapters.gateway.tasks import GatewayTransportError
from super_xd.nlp.orchestration.tasks.service import TaskService
from tests.support.orchestration import StreamModel, message, request, kernel, outputs


async def test_uncertain_delivery_does_not_downgrade_confirmed_registration(monkeypatch):
    ledger = MemoryTaskLedger()
    tasks = TaskService(ledger, lambda store: None, model_policy_id='models-test')
    async def uncertain_delivery(*args, **kwargs):
        raise GatewayTransportError('gateway_transport_failure', uncertain=True)
    monkeypatch.setattr(tasks, 'await_result', uncertain_delivery)
    args = {'goal': '核实两份公开材料', 'mode': 'foreground'}
    model = StreamModel({**message('delegate_task', args), 'content': '我查一下。'})
    start = request('核实两份公开材料')
    core = kernel(model, start, task_service=tasks)
    tasks.model_policy_id = core.model_policy_id
    last = (await outputs(core.start(start)))[-1]
    assert len(ledger.tasks) == 1
    commit = last.checkpoint.orchestration.groups[0].task_commit
    assert commit.status == 'confirmed'
    assert last.error_code != 'task_commit_unknown'


async def test_foreground_delivery_respects_explicit_full_reading_preference():
    class CompletedLedger(MemoryTaskLedger):
        async def read(self, task_id):
            task = await super().read(task_id)
            if task:
                task.update(status='completed', result={'summary': '完整条件：周六开放，周日需要预约。',
                                                       'spoken_summary': '周末可以参观。'})
                self.root.tasks[task_id] = task
            return task
    ledger = CompletedLedger()
    tasks = TaskService(ledger, lambda store: None, model_policy_id='models-test')
    args = {'goal': '核实周末开放条件并详细读出', 'mode': 'foreground', 'read_details': True}
    model = StreamModel({**message('delegate_task', args), 'content': '我核实一下。'})
    start = request('详细读出周末开放条件')
    core = kernel(model, start, task_service=tasks)
    tasks.model_policy_id = core.model_policy_id
    last = (await outputs(core.start(start)))[-1]
    assert '周日需要预约' in last.presentation.speech
    assert len(model.inputs) == 1


async def test_foreground_cancellation_keeps_registration_confirmed_and_detaches_once(monkeypatch):
    import asyncio
    ledger = MemoryTaskLedger()
    tasks = TaskService(ledger, lambda store: None, model_policy_id='models-test')
    entered = asyncio.Event()
    wait = tasks.await_result
    async def foreground_wait(*args, **kwargs):
        entered.set()
        return await wait(*args, **kwargs)
    monkeypatch.setattr(tasks, 'await_result', foreground_wait)
    model = StreamModel({**message('delegate_task', {'goal': '核实公开资料', 'mode': 'foreground'}),
                         'content': '我查一下。'})
    start = request('核实公开资料')
    core = kernel(model, start, task_service=tasks)
    tasks.model_policy_id = core.model_policy_id
    run = core.start(start)
    collecting = asyncio.create_task(outputs(run))
    await asyncio.wait_for(entered.wait(), 1)
    run.cancel()
    last = (await collecting)[-1]
    assert last.type == 'run.cancelled'
    assert last.checkpoint.orchestration.groups[0].task_commit.status == 'confirmed'
    assert len(ledger.tasks) == 1
    assert next(iter(ledger.tasks.values()))['delivery_mode'] == 'background'
    assert len(model.inputs) == 1
