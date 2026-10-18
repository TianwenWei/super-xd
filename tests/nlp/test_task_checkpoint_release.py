"""A resumed Thinker must not silently inherit a different prompt/code release."""
from copy import deepcopy

import pytest

from super_xd.nlp.release import get_release_id
from tests.support.task_runner import MemoryTaskStore, ScriptedModel, message, runner


async def paused_task():
    store = MemoryTaskStore()
    core = runner(store, ScriptedModel(message('ask_clarification', {'question': '选择哪个范围？'})))
    await core.run_task(await store.read('task'))
    assert store.task['status'] == 'waiting_clarification'
    return store


async def test_task_checkpoint_carries_current_release_and_format_version():
    store = await paused_task()
    assert store.task['checkpoint']['release_id'] == get_release_id()
    assert store.task['checkpoint']['checkpoint_version'] == 1


@pytest.mark.parametrize('change', [
    {'release_id': 'different-release'}, {'checkpoint_version': 2},
    {'release_id': None, 'checkpoint_version': None},
])
async def test_incompatible_task_checkpoint_fails_without_calling_model_or_erasing_evidence(change):
    store = await paused_task()
    store.task['checkpoint'].update(change)
    before = deepcopy(store.task['checkpoint'])
    store.task['status'] = 'queued'
    store.task['revision'] += 1
    model = ScriptedModel()
    await runner(store, model).run_task(await store.read('task'))
    assert store.task['status'] == 'failed'
    assert 'checkpoint release/version mismatch' in store.task['error']
    assert store.task['checkpoint'] == before
    assert not model.messages


async def test_finalization_rejects_incompatible_checkpoint_before_model_call():
    store = await paused_task()
    store.task['checkpoint']['release_id'] = 'different-release'
    model = ScriptedModel()
    with pytest.raises(ValueError) as failure:
        await runner(store, model)._finalize(await store.read('task'))
    assert 'checkpoint release/version mismatch' in str(failure.value)
    assert not model.messages
