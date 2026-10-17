"""TaskRunner contract tests with caller-owned memory state and no service clients."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from backend.harness.task_runner import RevisionChanged, TaskRunner
from backend.harness.task_store import running_elapsed


def message(name, arguments, call_id='call-1'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': call_id, 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(arguments)},
    }]}


class MemoryTaskStore:
    """One fixture implements both ports over a shared caller-owned event ledger."""

    def __init__(self, *, context=None):
        now = datetime.now(timezone.utc)
        self.task = {'id': 'task', 'session_id': 'context', 'goal': '核实公开资料',
                     'revision': 1, 'status': 'running', 'constraints': [],
                     'context': context or {}, 'steps': 0, 'checkpoint': {},
                     'running_seconds': 0, 'running_since': now.isoformat(),
                     'deadline_at': (now + timedelta(seconds=60)).isoformat(),
                     'sources': [], 'transcript': [], 'result': None}
        self.events = []

    async def read(self, task_id):
        await asyncio.sleep(0)
        return deepcopy(self.task) if task_id == self.task['id'] else None

    async def refresh(self, task_id):
        return await self.read(task_id)

    async def save(self, task_id, revision, **fields):
        await asyncio.sleep(0)
        if task_id != self.task['id'] or revision != self.task['revision']:
            return False
        if self.task['status'] in {'completed', 'failed', 'cancelled'}:
            return False
        if self.task['status'] == 'cancelling' and fields.get('status') != 'cancelled':
            return False
        status = fields.get('status', self.task['status'])
        if status != self.task['status']:
            self.task['running_seconds'] = running_elapsed(self.task)
            self.task['running_since'] = datetime.now(timezone.utc).isoformat() if status == 'running' else None
        self.task.update(deepcopy(fields))
        return True

    async def expire(self, task_id, max_age_seconds):
        task = await self.read(task_id)
        if task and (running_elapsed(task) >= max_age_seconds or
                     datetime.fromisoformat(task['deadline_at']) <= datetime.now(timezone.utc)):
            await self.save(task_id, task['revision'], status='failed', error_code='deadline')

    async def notify_progress(self, task_id):
        return False

    async def usage(self, task_id, session_id):
        rows = [row for row in self.events if row.get('task_id') == task_id and row['session_id'] == session_id]
        elapsed = sum(row['data'].get('elapsed_seconds', 0) for row in rows
                      if row['kind'] == 'task.execution.finished' and row['data'].get('query_mode') == 'quick')
        lookups = sum(row['kind'] == 'tool.started' and row['data'].get('name') == 'lookup_web' for row in rows)
        return elapsed, lookups

    async def emit(self, session_id, kind, data, **fields):
        await asyncio.sleep(0)
        event = deepcopy({'session_id': session_id, 'kind': kind, 'data': data, **fields})
        self.events.append(event)
        return event


class ScriptedModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.messages = []

    async def complete(self, messages, **kwargs):
        self.messages.append(deepcopy(messages))
        response = self.responses.pop(0)
        return await response(messages) if callable(response) else response


class MemorySearch:
    def __init__(self):
        self.calls = []

    async def search(self, query, max_results=5, **options):
        self.calls.append(('search', query))
        return {'results': [{'url': 'https://example.org/source', 'title': '来源', 'content': '线索'}]}

    async def extract(self, urls):
        self.calls.append(('extract', urls))
        return {'results': [{'url': url, 'raw_content': '已实际读取的资料'} for url in urls]}


def runner(store, model, search=None, **options):
    return TaskRunner(store, model, search or MemorySearch(), trace_sink=store, **options)


def test_core_import_does_not_load_database_worker_or_provider_transports():
    root = Path(__file__).resolve().parents[1]
    command = ('import sys; import backend.harness.task_runner; '
               'assert not {"backend.db", "backend.worker", "backend.models", '
               '"backend.services.factory"}.intersection(sys.modules)')
    result = subprocess.run([sys.executable, '-c', command], cwd=root, capture_output=True, text=True,
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHON_DOTENV_DISABLED': '1'})
    assert result.returncode == 0, result.stderr


def test_task_prompt_capture_uses_explicit_clock():
    core = object.__new__(TaskRunner)
    messages = core._messages(MemoryTaskStore().task, current_utc='2026-10-17T12:00:00+00:00')
    assert json.loads(messages[-1]['content'])['current_utc'] == '2026-10-17T12:00:00+00:00'


@pytest.mark.asyncio
async def test_memory_store_completes_research_with_injected_tools_and_trace():
    store, search = MemoryTaskStore(), MemorySearch()
    model = ScriptedModel(message('search_web', {'query': '公开资料'}),
                          message('extract_web', {'urls': ['https://example.org/source']}),
                          message('finish_task', {'summary': '已核实资料。'}))
    await runner(store, model, search).run_task(await store.read('task'))
    saved = await store.read('task')
    assert saved['status'] == 'completed'
    assert saved['result']['web_evidence'] == 'extracted'
    assert saved['checkpoint']['tool_count'] == 2
    assert search.calls == [('search', '公开资料'), ('extract', ['https://example.org/source'])]
    assert any(row['kind'] == 'model.completed' for row in store.events)
    assert any(row['kind'] == 'tool.completed' and row['data']['name'] == 'finish_task' for row in store.events)


@pytest.mark.asyncio
async def test_new_runner_resumes_saved_checkpoint_after_cancellation_without_repeating_search():
    store, first_search = MemoryTaskStore(), MemorySearch()
    waiting = asyncio.Event()

    async def blocked(messages):
        waiting.set()
        await asyncio.Event().wait()

    first = runner(store, ScriptedModel(message('search_web', {'query': '原查询'}), blocked), first_search)
    pending = asyncio.create_task(first.run_task(await store.read('task')))
    await asyncio.wait_for(waiting.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    checkpoint = deepcopy(store.task['checkpoint'])
    assert checkpoint['sources'][0]['url'] == 'https://example.org/source'
    assert checkpoint['tool_count'] == 1

    # A fresh adapter and runner receive only serialized task state and events.
    resumed = MemoryTaskStore()
    resumed.task = json.loads(json.dumps(store.task))
    resumed.events = json.loads(json.dumps(store.events))
    search = MemorySearch()
    model = ScriptedModel(message('extract_web', {'urls': ['https://example.org/source']}),
                          message('finish_task', {'summary': '续跑读取后完成。'}))
    await runner(resumed, model, search).run_task(await resumed.read('task'))
    assert resumed.task['status'] == 'completed'
    assert resumed.task['steps'] == 4
    assert resumed.task['checkpoint']['tool_count'] == 2
    assert search.calls == [('extract', ['https://example.org/source'])]
    assert any(item.get('role') == 'tool' and '线索' in item['content'] for item in model.messages[0])


@pytest.mark.asyncio
async def test_revision_changed_during_tool_call_cannot_commit_old_result():
    store = MemoryTaskStore()

    class UpdatingSearch(MemorySearch):
        async def search(self, *args, **kwargs):
            result = await super().search(*args, **kwargs)
            store.task['revision'] += 1
            store.task['constraints'].append('新的范围')
            return result

    model = ScriptedModel(message('search_web', {'query': '旧条件'}),
                          message('finish_task', {'summary': '新范围尚未查询。'}))
    await runner(store, model, UpdatingSearch()).run_task(await store.read('task'))
    assert store.task['status'] == 'completed' and store.task['revision'] == 2
    assert store.task['sources'] == []
    assert store.task['result']['summary'] == '新范围尚未查询。'
    assert any(row['kind'] == 'tool.completed' and row['status'] == 'stale' for row in store.events)


@pytest.mark.asyncio
async def test_compare_and_set_failure_is_not_accepted_as_a_checkpoint():
    store = MemoryTaskStore()
    core = runner(store, ScriptedModel())
    store.task['revision'] = 2
    with pytest.raises(RevisionChanged) as changed:
        await core._save('task', 1, status='completed', result={'summary': '旧结果'})
    assert changed.value.task['revision'] == 2
    assert store.task['status'] == 'running' and store.task['result'] is None


@pytest.mark.asyncio
async def test_resumed_quick_run_keeps_caller_recorded_execution_budget():
    store = MemoryTaskStore(context={'query_mode': 'quick'})
    await store.emit('context', 'task.execution.finished',
                     {'query_mode': 'quick', 'elapsed_seconds': 1}, task_id='task')
    model = ScriptedModel()
    await runner(store, model, quick_timeout_seconds=.5).run_task(await store.read('task'))
    assert not model.messages
    assert store.task['status'] == 'failed' and store.task['error_code'] == 'deadline'
    timeout = next(row for row in store.events if row['kind'] == 'task.timeout')
    assert timeout['data']['reason'] == 'quick_execution_budget'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['quick', 'research'])
async def test_replayed_waiting_task_preserves_question_and_consumes_no_budget(mode):
    store = MemoryTaskStore(context={'query_mode': mode})
    stale = await store.read('task')
    store.task.update(status='waiting_clarification', question='需要哪座城市？',
                      running_since=None, deadline_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    waiting = deepcopy(store.task)
    model = ScriptedModel()
    await runner(store, model).run_task(stale)
    assert store.task == waiting
    assert not model.messages and not store.events


@pytest.mark.asyncio
async def test_stale_deadline_cannot_fail_new_revision_after_clarification_reply():
    store = MemoryTaskStore()
    stale = await store.read('task')
    stale['deadline_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    store.task['revision'] = 2
    store.task['constraints'] = ['用户已回复新条件']
    model = ScriptedModel(message('finish_task', {'summary': '根据新条件完成。'}))
    await runner(store, model).run_task(stale)
    assert store.task['status'] == 'completed' and store.task['revision'] == 2
    assert len(model.messages) == 1
    request = json.loads(model.messages[0][-1]['content'])
    assert request['revision'] == 2 and request['constraints'] == ['用户已回复新条件']
    assert not any(row['kind'] == 'task.timeout' for row in store.events)


@pytest.mark.asyncio
async def test_cancelling_task_still_commits_cancellation_before_model_execution():
    store = MemoryTaskStore()
    stale = await store.read('task')
    store.task.update(status='cancelling', question='旧问题')
    model = ScriptedModel()
    await runner(store, model).run_task(stale)
    assert store.task['status'] == 'cancelled' and store.task['question'] is None
    assert not model.messages


@pytest.mark.asyncio
async def test_core_weather_configuration_never_constructs_an_adapter():
    store = MemoryTaskStore()
    core = runner(store, ScriptedModel())
    with pytest.raises(RuntimeError) as failure:
        await core._execute_tool('get_weather_forecast', {'city': 'Paris', 'date': '2026-10-17'}, {}, [], [])
    assert str(failure.value) == 'Weather tool is not configured'


@pytest.mark.asyncio
async def test_no_progress_limit_survives_fresh_runner_and_does_not_call_provider_again():
    store, search, waiting = MemoryTaskStore(), MemorySearch(), asyncio.Event()

    async def blocked(messages):
        waiting.set()
        await asyncio.Event().wait()

    first = runner(store, ScriptedModel(message('search_web', {'query': '首次查询'}, 'one'),
                                        message('search_web', {'query': '改写查询'}, 'two'), blocked), search)
    execution = asyncio.create_task(first.run_task(await store.read('task')))
    await asyncio.wait_for(waiting.wait(), 2)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    saved = json.loads(json.dumps(store.task))
    assert len(saved['checkpoint']['retrieval']['records']) == 2
    resumed = MemoryTaskStore()
    resumed.task, resumed.events = saved, deepcopy(store.events)
    fresh_search = MemorySearch()
    model = ScriptedModel(message('search_web', {'query': '再换一种表述'}, 'three'),
                          message('extract_web', {'urls': ['https://example.org/source']}, 'read'),
                          message('finish_task', {'summary': '已读取已有来源，其他内容尚无法确认。'}))
    await runner(resumed, model, fresh_search).run_task(await resumed.read('task'))
    assert fresh_search.calls == [('extract', ['https://example.org/source'])]
    assert resumed.task['status'] == 'completed', (resumed.task.get('error_code'), resumed.task.get('summary'), resumed.events[-3:])
    rejected = next(json.loads(row['content']) for row in model.messages[1]
                    if row.get('tool_call_id') == 'three')
    assert rejected['executed'] is False and rejected['next_step'] == 'answer_with_available_evidence'
    assert len(resumed.task['checkpoint']['retrieval']['records']) == 3
