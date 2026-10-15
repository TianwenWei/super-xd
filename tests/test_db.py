"""SQLite concurrency and optimistic-version guarantees, without external APIs."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import threading

import pytest

from backend.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "ledger.sqlite3")


def test_wal_is_enabled_and_claims_are_unique_across_worker_connections(db):
    tasks = [db.create_task("session", f"任务{i}") for i in range(16)]
    with db.connection() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    # Each method opens a distinct connection; the claim transaction must serialize
    # its read+write so another worker cannot receive the same queued row.
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda _: db.claim_next(), range(16)))
    assert {entry["id"] for entry in claimed} == {entry["id"] for entry in tasks}
    assert len({entry["id"] for entry in claimed}) == 16
    assert all(entry["status"] == "running" for entry in claimed)
    assert db.claim_next() is None


def test_concurrent_expected_revision_writers_have_exactly_one_winner(db):
    current = db.create_task("session", "原始目标")
    barrier = threading.Barrier(2)

    def update(text):
        barrier.wait()
        try:
            return db.mutate_task(current["id"], "update", text, expected_revision=1)
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["条件A", "条件B"]))
    assert len([entry for entry in results if isinstance(entry, dict)]) == 1
    conflicts = [entry for entry in results if isinstance(entry, str)]
    assert len(conflicts) == 1 and "版本冲突" in conflicts[0]
    saved = db.get_task(current["id"])
    assert saved["revision"] == 2
    assert len(saved["constraints"]) == 1
    assert saved["constraints"][0] in {"条件A", "条件B"}


def test_concurrent_updates_without_expected_revision_preserve_all_conditions(db):
    current = db.create_task("session", "原始目标")
    conditions = [f"条件{i}" for i in range(24)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda text: db.mutate_task(current["id"], "update", text), conditions))
    saved = db.get_task(current["id"])
    assert saved["revision"] == 25
    assert set(saved["constraints"]) == set(conditions)
    assert len(saved["constraints"]) == 24
    inbox = db.inbox(current["id"])
    assert [entry["revision"] for entry in inbox] == list(range(2, 26))
    assert db.inbox(current["id"]) == []


def test_old_worker_revision_cannot_overwrite_updated_conditions(db):
    current = db.create_task("session", "查询目标")
    assert db.claim_next()["id"] == current["id"]
    changed = db.mutate_task(current["id"], "update", "新增限制")
    assert not db.save_task(current["id"], 1, status="completed", result={"summary": "旧结果"})
    saved = db.apply_pending(current["id"])
    assert saved["revision"] == changed["revision"] == 2
    assert saved["result"] is None
    assert saved["constraints"] == ["新增限制"]
    assert db.inbox(current["id"]) == []
    assert db.save_task(current["id"], 2, status="completed", result={"summary": "新结果"})


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_terminal_task_rejects_late_worker_save_and_resurrection(db, status):
    current = db.create_task("session", "目标")
    assert db.save_task(current["id"], 1, status=status, summary="最终状态")
    before_events = len(db.events_after(0))
    assert not db.save_task(current["id"], 1, status="running", summary="迟到进度")
    assert not db.save_task(current["id"], 1, status="completed", result={"summary": "迟到结果"})
    saved = db.get_task(current["id"])
    assert saved["status"] == status
    assert saved["summary"] == "最终状态"
    assert len(db.events_after(0)) == before_events


def test_user_explicit_update_requeues_completed_task_with_new_revision(db):
    current = db.create_task("session", "目标")
    assert db.save_task(current["id"], 1, status="completed", result={"summary": "旧结果"})
    updated = db.mutate_task(current["id"], "update", "再加条件", expected_revision=1)
    assert updated["revision"] == 2
    assert updated["status"] == "queued"
    assert updated["result"] is None
    assert not db.save_task(current["id"], 1, status="completed", result={"summary": "旧结果"})
    with pytest.raises(ValueError, match="版本冲突"):
        db.mutate_task(current["id"], "cancel", expected_revision=1)


def test_cancelled_queue_entry_can_be_excluded_so_another_slot_claims_next(db):
    first = db.create_task("session", "先取消")
    cancelled = db.mutate_task(first["id"], "cancel", expected_revision=1)
    second = db.create_task("session", "新任务")
    assert cancelled["status"] == "cancelling"
    assert db.claim_next()["id"] == first["id"]
    assert db.claim_next(exclude_ids=(first["id"],))["id"] == second["id"]
    assert not db.save_task(first["id"], 1, status="completed", summary="旧完成事件")
    assert not db.save_task(first["id"], 2, status="running", summary="禁止取消中复活")
    assert db.save_task(first["id"], 2, status="cancelled", summary="已取消")
    assert db.claim_next() is None
    with pytest.raises(ValueError, match="已取消"):
        db.mutate_task(first["id"], "update", "新条件")


@pytest.mark.parametrize("action", ["update", "reply", "cancel"])
def test_cancelling_task_rejects_inbox_mutations_instead_of_reversing_cancel(db, action):
    current = db.create_task("session", "目标")
    cancelled = db.mutate_task(current["id"], "cancel", expected_revision=1)
    with pytest.raises(ValueError, match="取消"):
        db.mutate_task(current["id"], action, "迟到条件", expected_revision=2)
    saved = db.get_task(current["id"])
    assert saved["revision"] == cancelled["revision"] == 2
    assert saved["status"] == "cancelling"
    assert saved["constraints"] == []


def test_task_events_are_ordered_cursor_readable_and_session_scoped(db):
    ours = db.create_task("ours", "我们的任务")
    other = db.create_task("other", "其他会话")
    db.save_task(ours["id"], 1, summary="当前进度")
    db.save_task(other["id"], 1, summary="其他进度")
    all_events = db.events_after(0)
    assert [item["event_id"] for item in all_events] == sorted(item["event_id"] for item in all_events)
    assert len({item["event_id"] for item in all_events}) == len(all_events)
    ours_events = db.events_after(0, "ours")
    assert len(ours_events) == 2
    assert all(item["session_id"] == "ours" and item["task_id"] == ours["id"] for item in ours_events)
    assert db.events_after(ours_events[-1]["event_id"], "ours") == []
    assert db.list_tasks("ours")[0]["id"] == ours["id"]
    assert db.get_task("unknown") is None


def test_expiration_uses_running_and_clarification_clocks_but_preserves_queue_and_terminal(db):
    statuses = ["queued", "running", "waiting_clarification", "cancelling", "completed"]
    tasks = {status: db.create_task("session", status) for status in statuses}
    for status in ["running", "waiting_clarification", "completed"]:
        assert db.save_task(tasks[status]["id"], 1, status=status)
    db.mutate_task(tasks["cancelling"]["id"], "cancel")
    old = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
    with db.connection() as connection:
        connection.execute("UPDATE tasks SET created_at=?", (old,))
        connection.execute("UPDATE tasks SET running_since=? WHERE status='running'", (old,))
        connection.execute("UPDATE tasks SET clarification_expires_at=? WHERE status='waiting_clarification'", (old,))
        connection.execute("UPDATE tasks SET updated_at=? WHERE status='cancelling'", (old,))
    assert db.expire_tasks(300) == 3
    for status in ["running", "waiting_clarification"]:
        saved = db.get_task(tasks[status]["id"])
        assert saved["status"] == "failed"
        assert "累计执行" in saved["error"] if status == "running" else "澄清等待" in saved["error"]
    assert db.get_task(tasks["cancelling"]["id"])["status"] == "cancelled"
    assert db.get_task(tasks["completed"]["id"])["status"] == "completed"
    assert db.get_task(tasks["queued"]["id"])["status"] == "queued"
    assert db.expire_tasks(300) == 0


def test_trace_roundtrip_is_persistent_complete_and_keeps_provider_observed_time(db):
    body = '完整模型与网页返回。\n' * 16000
    observed = '2026-10-11T15:00:00.123456+00:00'
    data = {'agent': 'thinker', 'observed_at': observed, 'message': {'content': body},
            'meta': {'usage': {'total_tokens': 4321}, 'raw_response': {'reasoning_content': body}}}
    event = db.append_trace('session', 'model.completed', data, turn_id='turn', task_id='task',
                            revision=3, call_id='provider-call', step=7, status='received')
    assert event == {'trace_id': event['trace_id'], 'session_id': 'session', 'turn_id': 'turn',
                     'task_id': 'task', 'revision': 3, 'call_id': 'provider-call', 'step': 7,
                     'kind': 'model.completed', 'status': 'received', 'created_at': observed, 'data': data}
    # Re-open the same database: traces are an independent durable ledger, not
    # an in-memory view of the latest task.transcript.
    reopened = Database(db.path)
    assert reopened.traces_after('session') == [event]
    event['data']['message']['content'] = 'caller mutation'
    assert reopened.traces_after('session')[0]['data']['message']['content'] == body


def test_trace_cursor_filters_are_ordered_session_scoped_and_composable(db):
    one = db.append_trace('ours', 'model.started', {'n': 1}, turn_id='t1', task_id='task1')
    db.append_trace('other', 'model.completed', {'private': True}, turn_id='t1', task_id='task1')
    two = db.append_trace('ours', 'model.completed', {'n': 2}, turn_id='t1', task_id='task2')
    three = db.append_trace('ours', 'tool.completed', {'n': 3}, turn_id='t2', task_id='task1')
    assert db.traces_after('ours', limit=2) == [one, two]
    assert db.traces_after('ours', after_id=two['trace_id']) == [three]
    assert db.traces_after('ours', turn_id='t1') == [one, two]
    assert db.traces_after('ours', task_id='task1') == [one, three]
    assert db.traces_after('ours', turn_id='t1', task_id='task1') == [one]
    assert db.traces_after('unknown') == []
    assert db.traces_after('ours', after_id=three['trace_id']) == []
    with pytest.raises(ValueError, match='正整数'):
        db.traces_after('ours', limit=0)


def test_trace_concurrent_writes_have_unique_monotonic_ids_and_complete_payloads(db):
    payloads = [{'index': i, 'text': '长结果' * 2000} for i in range(40)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        written = list(pool.map(lambda data: db.append_trace('session', 'tool.completed', data), payloads))
    items = db.traces_after('session')
    ids = [item['trace_id'] for item in items]
    assert len(set(ids)) == len(payloads)
    assert ids == sorted(ids)
    assert {item['trace_id'] for item in written} == set(ids)
    assert sorted((item['data'] for item in items), key=lambda data: data['index']) == payloads


def test_trace_redacts_authentication_without_truncating_ordinary_data(db, monkeypatch):
    monkeypatch.setenv('ARK_API_KEY', 'actual-ark-secret')
    data = {'Authorization': 'Bearer actual-ark-secret', 'headers': {'X-Api-Key': 'speech-secret'},
            'meta': {'api_key': 'tavily-secret', 'access_token': 'access-secret'},
            'diagnostic': 'provider exposed actual-ark-secret', 'body': '完整正文' * 20000}
    saved = db.append_trace('session', 'model.error', data)
    encoded = json.dumps(saved, ensure_ascii=False)
    assert all(secret not in encoded for secret in ['actual-ark-secret', 'speech-secret', 'tavily-secret', 'access-secret'])
    assert saved['data']['body'] == data['body']
    assert data['Authorization'] == 'Bearer actual-ark-secret'


def test_task_lifecycle_trace_preserves_all_revisions_and_rejected_save_writes_nothing(db):
    current = db.create_task('session', '原始目标')
    db.claim_next()
    db.save_task(current['id'], 1, status='waiting_clarification', question='哪个城市？')
    db.mutate_task(current['id'], 'reply', '上海')
    db.claim_next()
    db.mutate_task(current['id'], 'update', '只看官方资料')
    db.mutate_task(current['id'], 'cancel')
    before = db.traces_after('session', task_id=current['id'])
    assert not db.save_task(current['id'], 1, status='completed', result={'summary': '迟到旧结果'})
    assert db.traces_after('session', task_id=current['id']) == before
    assert db.save_task(current['id'], 4, status='cancelled')
    items = db.traces_after('session', task_id=current['id'])
    assert [item['kind'] for item in items] == ['task.created', 'task.running',
        'task.waiting_clarification', 'task.reply', 'task.running', 'task.update', 'task.cancel', 'task.cancelled']
    assert [item['revision'] for item in items] == [1, 1, 1, 2, 2, 3, 4, 4]
    assert items[2]['data']['task']['question'] == '哪个城市？'
    assert items[3]['data']['task']['context']['clarification_history'][0]['answer'] == '上海'
    assert items[-1]['status'] == 'cancelled'


def test_task_event_and_lifecycle_trace_commit_or_rollback_together(db):
    current = db.create_task('session', '目标')
    events = db.events_after(0)
    traces = db.traces_after('session')
    with pytest.raises(RuntimeError, match='rollback'):
        with db.connection() as connection:
            db._event(connection, current, 'progress', {'marker': 'must rollback'})
            raise RuntimeError('rollback')
    assert db.events_after(0) == events
    assert db.traces_after('session') == traces


def test_existing_ledger_migrates_trace_table_without_changing_tasks(db):
    current = db.create_task('session', '升级前任务')
    with db.connection() as connection:
        connection.execute('DROP TABLE trace_events')
    reopened = Database(db.path)
    assert reopened.get_task(current['id']) == current
    assert reopened.traces_after('session') == []
    assert reopened.append_trace('session', 'model.started', {})['trace_id'] > 0


def test_task_lifecycle_trace_links_origin_turn_but_manual_tasks_remain_unlinked(db):
    delegated = db.create_task('session', '来自前台的任务', {'origin_turn_id': 'delegate-turn'})
    manual = db.create_task('session', '手工任务')
    db.save_task(delegated['id'], 1, summary='继续处理')
    assert all(item['turn_id'] == 'delegate-turn'
               for item in db.traces_after('session', task_id=delegated['id']))
    assert all(item['turn_id'] is None for item in db.traces_after('session', task_id=manual['id']))
