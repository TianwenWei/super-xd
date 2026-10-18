"""Actor-owned notification queue; all mutations happen inside handle()."""
from datetime import datetime, timedelta, timezone

from super_xd.nlp.contracts.events import SpeakingContext, TaskNotice
from super_xd.nlp.policy.speaking import decide_presentation, notice_order


class NotificationQueue:
    def __init__(self):
        self.pending: dict[str, TaskNotice] = {}
        self.delivered: set[tuple[str, int, str]] = set()
        self.inflight: set[str] = set()
        self.retry_attempts: dict[str, int] = {}
        self.retry_after: dict[str, datetime] = {}

    @staticmethod
    def key(notice):
        return notice.task_id, notice.revision, notice.kind if notice.kind == 'progress' else notice.status

    def offer(self, task, *, event_id, progress=False, now=None):
        now = now or datetime.now(timezone.utc)
        kind = ('progress' if progress else {'completed': 'result', 'failed': 'failed',
            'cancelled': 'cancelled', 'waiting_clarification': 'clarification'}.get(task['status']))
        if kind is None:
            return
        expires = now + timedelta(minutes=10)
        if kind == 'clarification' and task.get('clarification_expires_at'):
            expires = datetime.fromisoformat(task['clarification_expires_at'])
        notice = TaskNotice(event_id=str(event_id), task_id=task['id'], revision=task['revision'],
            status=task['status'], kind=kind, priority=1 if kind == 'clarification' else 3 if progress else 2,
            created_at=now, expires_at=expires)
        previous = self.pending.get(task['id'])
        if self.key(notice) in self.delivered or previous and previous.revision > notice.revision:
            return
        # Repeated delivery cannot extend a notification's lifetime.
        if previous and self.key(previous) == self.key(notice):
            return
        if previous:
            self.discard(task['id'])
        self.pending[task['id']] = notice

    def is_pending(self, notice):
        return self.pending.get(notice.task_id) == notice and self.key(notice) not in self.delivered

    def _clear_attempt(self, notice):
        self.inflight.discard(notice.event_id)
        self.retry_attempts.pop(notice.event_id, None)
        self.retry_after.pop(notice.event_id, None)

    def discard(self, task_id):
        notice = self.pending.pop(task_id, None)
        if notice:
            self._clear_attempt(notice)

    def acknowledge(self, notice):
        self.delivered.add(self.key(notice))
        self._clear_attempt(notice)
        if self.pending.get(notice.task_id) == notice:
            self.pending.pop(notice.task_id)

    def delivered_in_turn(self, task):
        self.delivered.add((task['id'], task['revision'], task['status']))
        notice = self.pending.get(task['id'])
        if notice and notice.revision == task['revision'] and notice.status == task['status']:
            self.discard(task['id'])

    def retry(self, notice, *, failed=False, now=None):
        self.inflight.discard(notice.event_id)
        if not self.is_pending(notice):
            return None
        now = now or datetime.now(timezone.utc)
        remaining = (notice.expires_at - now).total_seconds()
        if remaining <= 0:
            self.acknowledge(notice)
            return None
        if not failed:
            return .25
        attempts = self.retry_attempts.get(notice.event_id, 0) + 1
        self.retry_attempts[notice.event_id] = attempts
        delay = min(.25 * 2 ** min(attempts - 1, 6), 15, remaining)
        self.retry_after[notice.event_id] = now + timedelta(seconds=delay)
        return delay

    def select(self, tasks, context: SpeakingContext, *, now=None):
        now = now or datetime.now(timezone.utc)
        for notice in sorted(list(self.pending.values()), key=lambda item: notice_order(item, now)):
            task = tasks.get(notice.task_id)
            if task is None:
                self.discard(notice.task_id)
                continue
            decision = decide_presentation(notice, context, now=now,
                current_revision=task['revision'], current_status=task['status'])
            if decision.action in {'drop', 'screen_only'}:
                self.acknowledge(notice)
            elif decision.action == 'speak' and notice.event_id not in self.inflight:
                if now < self.retry_after.get(notice.event_id, now):
                    continue
                self.retry_after.pop(notice.event_id, None)
                self.inflight.add(notice.event_id)
                return notice
        return None
