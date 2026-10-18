"""Foreground delivery observes the same durable task without another model call."""
from copy import deepcopy

import pytest

from evals.gateway_driver import MemoryTaskLedger
from super_xd.nlp.orchestration.tasks.service import TaskService
from tests.support.orchestration import StreamModel, message, request, kernel, outputs


class FinishingLedger(MemoryTaskLedger):
    async def read(self, task_id):
        value = await super().read(task_id)
        if value and value['status'] == 'queued' and self.root.outcome:
            value.update(deepcopy(self.root.outcome))
            self.root.tasks[task_id] = deepcopy(value)
        return value


@pytest.mark.parametrize('outcome,expected', [
    ({'status': 'completed', 'result': {'summary': '完整报告', 'spoken_summary': '找到了两条有来源的路线。'}}, '两条'),
    ({'status': 'waiting_clarification', 'question': '目的地是哪个城市？'}, '城市'),
    ({'status': 'failed', 'error_code': 'step_budget'}, '查询'),
    (None, '继续'),
])
async def test_foreground_result_and_detachment_use_one_registered_task(outcome, expected):
    ledger = FinishingLedger()
    ledger.outcome = outcome
    tasks = TaskService(ledger, lambda store: None, model_policy_id='models-test', foreground_wait_seconds=.01)
    args = {'goal': '比较两条路线的公开资料', 'mode': 'foreground'}
    model = StreamModel({**message('delegate_task', args), 'content': '我查一下。'})
    start = request('比较两条路线的公开资料')
    core = kernel(model, start, task_service=tasks)
    tasks.model_policy_id = core.model_policy_id
    last = (await outputs(core.start(start)))[-1]
    assert last.type == 'run.completed'
    assert expected in last.presentation.speech
    assert len(model.inputs) == 1
    assert len(ledger.tasks) == 1
    task = next(iter(ledger.tasks.values()))
    assert task['revision'] == 1
    assert task['delivery_mode'] == ('foreground' if outcome else 'background')
    observation = next(iter(last.checkpoint.orchestration.groups[0].results.values())).result
    assert observation['task']['id'] == task['id'] and observation['registered']
