"""Delivery arbitration uses the clarification deadline, not paused execution time."""
from datetime import datetime, timedelta, timezone

from super_xd.demo.session.notifications import NotificationQueue
from super_xd.nlp.contracts.events import SpeakingContext


def test_clarification_stays_deliverable_while_execution_deadline_is_paused():
    now = datetime.now(timezone.utc)
    task = {'id': 'task', 'revision': 1, 'status': 'waiting_clarification',
            'deadline_at': (now + timedelta(seconds=2)).isoformat(),
            'clarification_expires_at': (now + timedelta(minutes=30)).isoformat()}
    queue = NotificationQueue()
    queue.offer(task, event_id='question', now=now)
    assert queue.select({'task': task}, SpeakingContext(in_call=True), now=now) is None
    notice = queue.select({'task': task}, SpeakingContext(), now=now + timedelta(seconds=20))
    assert notice is not None and notice.kind == 'clarification'


def test_failed_presentation_retries_with_bounded_backoff_then_expires():
    now = datetime.now(timezone.utc)
    task = {'id': 'task', 'revision': 1, 'status': 'completed'}
    queue = NotificationQueue()
    queue.offer(task, event_id='result', now=now)
    notice = queue.select({'task': task}, SpeakingContext(), now=now)
    first = queue.retry(notice, failed=True, now=now)
    assert first == .25 and queue.is_pending(notice)
    assert queue.select({'task': task}, SpeakingContext(), now=now) is None
    second_at = now + timedelta(seconds=first)
    assert queue.select({'task': task}, SpeakingContext(), now=second_at) == notice
    assert queue.retry(notice, failed=True, now=second_at) == .5
    for _ in range(20):
        assert queue.retry(notice, failed=True, now=second_at) <= 15
    queue.select({'task': task}, SpeakingContext(), now=notice.expires_at)
    assert not queue.pending and not queue.retry_after and not queue.retry_attempts


async def test_transient_presentation_failure_retries_instead_of_losing_result(make_actor, monkeypatch):
    from tests.support.session import wait_for
    actor, q, _ = await make_actor([])
    present = actor.nlp.present_task
    calls = []
    async def flaky(request):
        calls.append(request)
        if len(calls) == 1:
            raise ConnectionError('temporary gateway read failure')
        return await present(request)
    monkeypatch.setattr(actor.nlp, 'present_task', flaky)
    task = actor.db.create_task(actor.id, '查询开放时间')
    actor.db.save_task(task['id'], 1, status='completed', result={'summary': '实际查到周末开放。'})
    done, _ = await wait_for(q, 'stream.done')
    assert '周末开放' in done['text'] and len(calls) == 2
    assert (task['id'], 1, 'completed') in actor.notifications.delivered


async def test_late_presentation_callback_cannot_repeat_already_delivered_result(make_actor, monkeypatch):
    import asyncio
    from tests.support.session import wait_for
    actor, q, _ = await make_actor([])
    post = actor.post
    events = []
    async def capture(event):
        if event['type'] == '_task_presented':
            events.append(event)
        return await post(event)
    monkeypatch.setattr(actor, 'post', capture)
    task = actor.db.create_task(actor.id, '查询开放时间')
    actor.db.save_task(task['id'], 1, status='completed', result={'summary': '实际查到周末开放。'})
    await wait_for(q, 'stream.done')
    await actor.post(events[0])
    await actor.queue.join()
    await asyncio.sleep(.05)
    assert len([turn for turn in actor.turns if turn.proactive]) == 1
    assert not any(event['type'] == 'stream.done' for event in list(q._queue))


from tests.support.session import make_actor  # noqa: E402,F401


async def test_demo_history_preserves_top_level_provider_continuation(make_actor):
    from super_xd.demo.session.state import Turn
    from tests.support.session import wait_for
    signature = {'google': {'thought_signature': 'opaque-test-signature'}}
    actor, q, _ = await make_actor([{'content': '收到。', 'extra_content': signature}])
    await actor.post({'type': 'input', 'text': '你好'})
    await wait_for(q, 'stream.done')
    following = Turn('following', '继续', actor.persona, actor.persona_version)
    start = actor.nlp_start_request(following)
    assistant = next(record for record in start.history[-1].records if record.role == 'assistant')
    assert assistant.provider_metadata == {'extra_content': signature}
    messages = actor.nlp.compile_start(start).messages
    assert any(row.get('extra_content') == signature for row in messages)
