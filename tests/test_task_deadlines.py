"""Task deadlines and spoken feedback use typed state, never diagnostic text."""
import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest

from backend.db import Database
from backend.session import Turn
from backend.harness.contracts import ToolRequest
from backend.model_context import compact_tool_result
from backend.worker import ThinkerWorker
from tests.test_worker import FakeArk, FakeTavily, message, task
from tests.test_session import make_actor, wait_for, sync_tasks


def age_task(db, task_id, seconds):
    at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    with db.connection() as con:
        con.execute('UPDATE tasks SET created_at=?,deadline_at=? WHERE id=?',
                    (at.isoformat(), (at + timedelta(seconds=60)).isoformat(), task_id))


def test_progress_and_deadline_are_atomic_once_and_include_queue(tmp_path):
    db = Database(tmp_path / 'deadlines.sqlite3')
    current = db.create_task('s', '查资料')
    age_task(db, current['id'], 31)
    assert db.notify_task_progress(current['id']) is True
    assert db.notify_task_progress(current['id']) is False
    notice = db.events_after(0, 's')[-1]
    assert notice['kind'] == 'progress_notice'
    assert notice['payload']['task']['progress_notified'] == 1
    age_task(db, current['id'], 61)
    assert db.expire_tasks(60) == 1
    saved = db.get_task(current['id'])
    assert saved['status'] == 'failed' and saved['error_code'] == 'deadline'
    assert db.events_after(0, 's')[-1]['payload']['task']['status'] == 'failed'
    assert not db.save_task(current['id'], 1, status='completed', summary='迟到结果')


def test_waiting_for_user_pauses_deadline_and_updates_do_not_reset_it(tmp_path):
    db = Database(tmp_path / 'deadlines.sqlite3')
    current = db.create_task('s', '需补充条件')
    original = current['deadline_at']
    db.save_task(current['id'], 1, status='waiting_clarification', question='哪个城市？')
    with db.connection() as con:
        con.execute('UPDATE tasks SET deadline_paused_at=? WHERE id=?',
                    ((datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(), current['id']))
    resumed = db.mutate_task(current['id'], 'reply', '广州')
    assert (datetime.fromisoformat(resumed['deadline_at']) - datetime.fromisoformat(original)).total_seconds() >= 120
    updated = db.mutate_task(current['id'], 'update', '只查官方来源')
    assert updated['deadline_at'] == resumed['deadline_at']


@pytest.mark.asyncio
async def test_worker_defaults_cap_even_old_config_at_one_minute(tmp_path):
    db = Database(tmp_path / 'worker.sqlite3')
    worker = ThinkerWorker(db, FakeArk(), FakeTavily(), timeout_seconds=300)
    assert worker.timeout_seconds == 60


@pytest.mark.asyncio
async def test_tool_budget_reserves_delivery_call(tmp_path):
    db = Database(tmp_path / 'worker.sqlite3')
    current = task(db, worker_options={'max_steps': 3})
    ark = FakeArk(message('search_web', {'query': '展馆参观规定'}),
                  message('extract_web', {'urls': ['https://example.org/official']}),
                  message('finish_task', {'summary': '真实页面已读取。', 'spoken_summary': '查到了页面资料。'}))
    await ThinkerWorker(db, ark, FakeTavily(), max_steps=3).run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'completed'
    assert [t['function']['name'] for t in ark.options[-1]['tools']] == ['finish_task']
    assert ark.options[-1]['thinking'] is False


@pytest.mark.asyncio
async def test_deadline_cancels_search_and_finalizes_saved_material(tmp_path):
    db = Database(tmp_path / 'worker.sqlite3')
    current = task(db, worker_options={'timeout_seconds': 0.3})
    cancelled = asyncio.Event()
    async def slow(messages):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
    ark = FakeArk(message('search_web', {'query': '展馆票务'}), slow,
                  message('finish_task', {'summary': '只找到搜索线索，页面尚未核实。',
                                         'spoken_summary': '页面还没能核实，请先以官方信息为准。', 'source_ids': []}))
    await ThinkerWorker(db, ark, FakeTavily(), timeout_seconds=.3).run_task(current)
    saved = db.get_task(current['id'])
    assert cancelled.is_set()
    assert saved['status'] == 'completed' and saved['result']['partial'] is True
    assert saved['sources'] and saved['result']['web_evidence'] == 'none'
    assert [t['function']['name'] for t in ark.options[-1]['tools']] == ['finish_task']
    assert len(ark.calls) == 3


@pytest.mark.asyncio
async def test_deadline_finalizer_timeout_preserves_sources_and_stops(tmp_path):
    db = Database(tmp_path / 'worker.sqlite3')
    current = task(db, worker_options={'timeout_seconds': 0.15})
    async def slow(messages):
        await asyncio.sleep(10)
    ark = FakeArk(message('search_web', {'query': '展馆票务'}), slow, slow)
    started = asyncio.get_running_loop().time()
    await ThinkerWorker(db, ark, FakeTavily(), timeout_seconds=.15).run_task(current)
    saved = db.get_task(current['id'])
    assert asyncio.get_running_loop().time() - started < .5
    assert saved['status'] == 'failed' and saved['error_code'] == 'deadline'
    assert saved['result']['partial'] and saved['result']['sources']
    assert saved['checkpoint']['messages']


@pytest.mark.asyncio
async def test_legacy_diagnostics_cannot_enter_task_speech_or_prompt(make_actor):
    actor, q, ark = await make_actor([])
    current = actor.db.create_task(actor.id, '调查参观安排')
    actor.db.save_task(current['id'], 1, status='running', summary='正在搜索：site:private.example/path query')
    sync_tasks(actor)
    start = actor.nlp_start_request(Turn('read-status', '进展如何', actor.persona, 1))
    reply = await actor.nlp.task_service.handle(start, ToolRequest(
        invocation_id='status', tool_call_id='status', idempotency_key='status',
        name='manage_task', arguments={'action': 'get', 'task_id': current['id']}))
    assert 'site:' not in json.dumps(compact_tool_result(reply, 'manage_task'))
    actor.db.save_task(current['id'], 1, status='failed', error='RuntimeError: secret diagnostic', summary='RuntimeError: secret diagnostic')
    done, _ = await wait_for(q, 'stream.done')
    assert 'RuntimeError' not in done['text'] and 'secret diagnostic' not in done['text']
    start = actor.nlp_start_request(Turn('next-turn', '进展如何', actor.persona, 1))
    prompt = json.dumps(actor.nlp.compile_start(start).messages, ensure_ascii=False)
    assert 'RuntimeError' not in prompt and 'site:private' not in prompt


@pytest.mark.asyncio
async def test_progress_notice_deferred_during_playback_and_delivered_once(make_actor):
    actor, q, ark = await make_actor([])
    await actor.post({'type': 'playback', 'speaking': True})
    await actor.queue.join()
    current = actor.db.create_task(actor.id, '研究行程')
    age_task(actor.db, current['id'], 31)
    assert actor.db.notify_task_progress(current['id'])
    await asyncio.sleep(.4)
    assert not any(e['type'] == 'turn.started' for e in list(q._queue))
    await actor.post({'type': 'playback', 'speaking': False})
    done, _ = await wait_for(q, 'stream.done')
    assert '还在' in done['text'] or '正在' in done['text']
    assert not actor.db.notify_task_progress(current['id'])
    assert not ark.requests


@pytest.mark.asyncio
async def test_interrupted_answer_history_uses_playback_metadata(make_actor):
    actor, q, ark = await make_actor([{'content': '这是完整的回答。后半段未播放。'}, {'content': '继续说明后半段。'}])
    await actor.post({'type': 'input', 'text': '介绍一下'})
    await wait_for(q, 'stream.done')
    previous = actor.current
    previous.playback_id = 'playback-test'
    previous.played_text = '这是完整的回答。'
    previous.audio_cancelled.set()
    await actor.post({'type': 'input', 'text': '继续说明'})
    await wait_for(q, 'stream.done')
    messages = ark.requests[-1]['messages']
    assert all('[朗读已打断]' not in (m.get('content') or '') for m in messages)
    assert '后半段未播放' in json.dumps(messages, ensure_ascii=False)
    snapshot = next(json.loads(row['content'].splitlines()[-1]) for row in messages
        if row['role'] == 'system' and row['content'].startswith('当前会话快照'))
    assert snapshot['playback_history'][-1] == {
        'turn_id': previous.id, 'heard_text': '这是完整的回答。', 'status': 'partial', 'interrupted': True}


@pytest.mark.asyncio
async def test_full_external_tool_budget_still_allows_finish(tmp_path):
    db = Database(tmp_path / 'worker.sqlite3')
    current = task(db, worker_options={'max_steps': 4})
    batch = {'role': 'assistant', 'content': None, 'tool_calls': [
        message('search_web', {'query': f'资料来源{i}'}, f'search-{i}')['tool_calls'][0] for i in range(4)]}
    ark = FakeArk(batch, message('finish_task', {'summary': '只有搜索线索，未能核实。',
                                               'spoken_summary': '目前仅找到线索，还没能核实。', 'source_urls': []}))
    await ThinkerWorker(db, ark, FakeTavily(), max_steps=4).run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'completed' and saved['checkpoint']['tool_count'] == 4
    assert len(ark.calls) == 2
    assert [t['function']['name'] for t in ark.options[-1]['tools']] == ['finish_task']


@pytest.mark.asyncio
async def test_terminal_result_supersedes_deferred_progress_notice(make_actor):
    actor, q, ark = await make_actor([])
    await actor.post({'type': 'playback', 'speaking': True})
    await actor.queue.join()
    current = actor.db.create_task(actor.id, '研究路线')
    age_task(actor.db, current['id'], 31)
    actor.db.notify_task_progress(current['id'])
    await asyncio.sleep(.2)
    actor.db.save_task(current['id'], 1, status='completed', result={'summary': '可以交付的结果。', 'partial': True})
    await asyncio.sleep(.2)
    await actor.post({'type': 'playback', 'speaking': False})
    done, _ = await wait_for(q, 'stream.done')
    assert '可以交付的结果' in done['text'] and '完整需求还没有全部核实' in done['text']
    await asyncio.sleep(.3)
    assert not any(e['type'] == 'stream.done' for e in list(q._queue))


@pytest.mark.asyncio
async def test_revision_update_refreshes_deferred_progress_without_old_speech(make_actor):
    actor, q, ark = await make_actor([])
    await actor.post({'type': 'playback', 'speaking': True})
    await actor.queue.join()
    current = actor.db.create_task(actor.id, '查展览')
    age_task(actor.db, current['id'], 31)
    actor.db.notify_task_progress(current['id'])
    await asyncio.sleep(.2)
    actor.db.mutate_task(current['id'], 'update', '只查明天')
    await asyncio.sleep(.2)
    await actor.post({'type': 'playback', 'speaking': False})
    await wait_for(q, 'stream.done')
    assert (current['id'], 2, 'progress_notice') in actor.announced_keys
    assert (current['id'], 1, 'progress_notice') not in actor.announced_keys


@pytest.mark.asyncio
async def test_finalizer_selects_verified_source_id_and_server_adds_actual_link(tmp_path):
    db = Database(tmp_path / 'worker.sqlite3')
    current = task(db, worker_options={'timeout_seconds': 0.4})
    async def slow(messages):
        await asyncio.sleep(10)
    async def finish(messages):
        catalog = json.loads(messages[-1]['content'])['citable_sources']
        assert len(catalog) == 1
        return message('finish_task', {'summary': '已有页面资料可以使用，其他部分尚未核实。',
                                      'spoken_summary': '有一部分资料可用，其他部分还没核实。',
                                      'source_ids': list(catalog)})
    ark = FakeArk(message('search_web', {'query': '展馆参观'}),
                  message('extract_web', {'urls': ['https://example.org/official']}), slow, finish)
    await ThinkerWorker(db, ark, FakeTavily(), timeout_seconds=.4).run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'completed' and saved['result']['partial']
    assert saved['result']['sources'][0]['url'] == 'https://example.org/official'
    assert 'https://example.org/official' in saved['result']['summary']
    assert 'https://' not in saved['result']['spoken_summary']


@pytest.mark.asyncio
async def test_invalid_finalizer_source_id_keeps_evidence_without_claiming_deadline(tmp_path):
    db = Database(tmp_path / 'worker.sqlite3')
    current = task(db, worker_options={'timeout_seconds': 0.3})
    async def slow(messages):
        await asyncio.sleep(10)
    ark = FakeArk(message('search_web', {'query': '展馆参观'}), slow,
                  message('finish_task', {'summary': '不应交付。', 'spoken_summary': '不应交付。', 'source_ids': ['invented']}))
    await ThinkerWorker(db, ark, FakeTavily(), timeout_seconds=.3).run_task(current)
    saved = db.get_task(current['id'])
    assert saved['status'] == 'failed' and saved['error_code'] == 'finalization_failed'
    assert saved['result']['partial'] and saved['sources']
    assert '时间上限' not in saved['summary'] and 'ValidationError' not in saved['summary']
