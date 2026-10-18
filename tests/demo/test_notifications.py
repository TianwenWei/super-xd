from datetime import datetime, timedelta, timezone

from super_xd.demo.session.notifications import NotificationQueue
from super_xd.nlp.contracts.events import SpeakingContext


NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


def test_duplicate_events_preserve_expiry_and_delivered_notice_does_not_repeat():
    queue = NotificationQueue()
    task = {'id': 't', 'revision': 1, 'status': 'completed'}
    queue.offer(task, event_id='one', now=NOW)
    expiry = queue.pending['t'].expires_at
    queue.offer(task, event_id='two', now=NOW + timedelta(minutes=1))
    assert queue.pending['t'].expires_at == expiry
    notice = queue.select({'t': task}, SpeakingContext(), now=NOW)
    assert queue.select({'t': task}, SpeakingContext(), now=NOW) is None
    queue.acknowledge(notice)
    queue.offer(task, event_id='three', now=NOW)
    assert not queue.pending


def test_latest_question_precedes_result_and_inflight_old_result_cannot_remove_it():
    queue = NotificationQueue()
    task = {'id': 't', 'revision': 1, 'status': 'completed'}
    queue.offer(task, event_id='one', now=NOW)
    old = queue.select({'t': task}, SpeakingContext(), now=NOW)
    task = {**task, 'revision': 2, 'status': 'waiting_clarification'}
    queue.offer(task, event_id='two', now=NOW)
    queue.acknowledge(old)
    assert queue.pending['t'].revision == 2
    assert queue.select({'t': task}, SpeakingContext(in_call=True), now=NOW) is None
    assert queue.select({'t': task}, SpeakingContext(), now=NOW).kind == 'clarification'


def test_progress_updates_screen_and_expired_result_is_dropped():
    queue = NotificationQueue()
    task = {'id': 't', 'revision': 1, 'status': 'running'}
    queue.offer(task, event_id='one', progress=True, now=NOW)
    assert queue.select({'t': task}, SpeakingContext(), now=NOW) is None
    assert not queue.pending
    task['status'] = 'completed'
    queue.offer(task, event_id='two', now=NOW)
    assert queue.select({'t': task}, SpeakingContext(), now=NOW + timedelta(minutes=11)) is None
    assert not queue.pending


def test_result_delivered_in_foreground_is_not_announced_by_late_event():
    queue = NotificationQueue()
    task = {'id': 't', 'revision': 1, 'status': 'completed'}
    queue.delivered_in_turn(task)
    queue.offer(task, event_id='late', now=NOW)
    assert queue.select({'t': task}, SpeakingContext(), now=NOW) is None
