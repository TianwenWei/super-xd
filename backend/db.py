"""SQLite WAL task ledger. Every operation owns a short connection/transaction."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4
from backend.settings import DATABASE_PATH
from backend.models import utcnow
from backend.textutil import redact_bearer, redact_known_secrets
from backend.task_feedback import partial_result, task_user_detail
from backend.speech_config import feedback_text

JSON_FIELDS = {'constraints', 'result', 'sources', 'transcript', 'context', 'checkpoint', 'progress'}
TERMINAL = {'completed', 'failed', 'cancelled'}


def task_card(task):
    """Task fields the actor and UI use; transcripts/checkpoints stay in the row."""
    return {key: value for key, value in task.items() if key not in {'transcript', 'checkpoint'}} if task else task


def step_summary(step):
    """Generic per-step progress text; the actor recognises it by equality, not parsing."""
    return feedback_text('task.db.step', step=step)


def _trace_data(value):
    """Keep complete JSON payloads while excluding credentials from diagnostics."""
    secret_fields = {'authorization', 'proxyauthorization', 'apikey', 'accesskey',
                     'accesstoken', 'appsecret', 'clientsecret', 'xapikey', 'xapiaccesskey', 'xgoogapikey', 'googlemapsapikey'}
    if isinstance(value, dict):
        return {key: ('[redacted]' if ''.join(c for c in str(key) if c not in '-_' and not c.isspace()).lower() in secret_fields
                      else _trace_data(item)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_trace_data(item) for item in value]
    if isinstance(value, str):
        return redact_bearer(redact_known_secrets(value))
    return value


class Database:
    def __init__(self, path=None):
        self.path = str(path or DATABASE_PATH)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            con.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, goal TEXT NOT NULL,
              constraints TEXT NOT NULL DEFAULT '[]', context TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'queued',
              revision INTEGER NOT NULL DEFAULT 1, summary TEXT NOT NULL DEFAULT '',
              result TEXT, sources TEXT NOT NULL DEFAULT '[]', transcript TEXT NOT NULL DEFAULT '[]',
              question TEXT, error TEXT, steps INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox (
              id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, action TEXT NOT NULL,
              text TEXT NOT NULL, revision INTEGER NOT NULL, consumed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
              event_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, session_id TEXT NOT NULL,
              revision INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trace_events (
              trace_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
              turn_id TEXT, task_id TEXT, revision INTEGER, call_id TEXT, step INTEGER,
              kind TEXT NOT NULL, status TEXT, created_at TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
            CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id,event_id);
            CREATE INDEX IF NOT EXISTS idx_trace_session ON trace_events(session_id,trace_id);
            CREATE INDEX IF NOT EXISTS idx_trace_turn ON trace_events(session_id,turn_id,trace_id);
            CREATE INDEX IF NOT EXISTS idx_trace_task ON trace_events(session_id,task_id,trace_id);
            ''')
            columns = {row[1] for row in con.execute('PRAGMA table_info(tasks)')}
            if 'context' not in columns:
                con.execute("ALTER TABLE tasks ADD COLUMN context TEXT NOT NULL DEFAULT '{}'")

            for name, sql_type in {'running_seconds': 'REAL NOT NULL DEFAULT 0',
                                   'running_since': 'TEXT', 'clarification_expires_at': 'TEXT',
                                   'checkpoint': "TEXT NOT NULL DEFAULT '{}'",
                                   'deadline_at': 'TEXT', 'deadline_paused_at': 'TEXT',
                                   'progress_notified': 'INTEGER NOT NULL DEFAULT 0',
                                   'progress': "TEXT NOT NULL DEFAULT '{}'", 'error_code': 'TEXT'}.items():
                if name not in columns:
                    con.execute(f'ALTER TABLE tasks ADD COLUMN {name} {sql_type}')
            for row in con.execute('SELECT id,created_at FROM tasks WHERE deadline_at IS NULL').fetchall():
                deadline = datetime.fromisoformat(row['created_at']) + timedelta(seconds=60)
                con.execute('UPDATE tasks SET deadline_at=? WHERE id=?', (deadline.isoformat(), row['id']))
            con.execute("UPDATE tasks SET deadline_paused_at=updated_at WHERE status='waiting_clarification' AND deadline_paused_at IS NULL")
            con.execute("UPDATE tasks SET running_since=updated_at WHERE status='running' AND running_since IS NULL")
            con.execute("UPDATE tasks SET clarification_expires_at=? WHERE status='waiting_clarification' AND clarification_expires_at IS NULL",
                        ((datetime.now(timezone.utc) + timedelta(seconds=self.clarification_timeout())).isoformat(),))

    @staticmethod
    def clarification_timeout():
        return float(os.getenv('THINKER_CLARIFICATION_TIMEOUT_SECONDS', '1800'))

    @staticmethod
    def running_elapsed(task, now=None):
        seconds = float(task.get('running_seconds') or 0)
        if task.get('status') == 'running' and task.get('running_since'):
            since = datetime.fromisoformat(task['running_since'])
            seconds += max(0, ((now or datetime.now(timezone.utc)) - since).total_seconds())
        return seconds

    def _transition_clock(self, con, task, status):
        if task['status'] == status:
            return
        now = datetime.now(timezone.utc)
        if task['status'] == 'waiting_clarification' and task.get('deadline_paused_at'):
            pause = now - datetime.fromisoformat(task['deadline_paused_at'])
            deadline = datetime.fromisoformat(task['deadline_at']) + pause
            con.execute('UPDATE tasks SET deadline_at=?,deadline_paused_at=NULL WHERE id=?',
                        (deadline.isoformat(), task['id']))
        if status == 'waiting_clarification':
            con.execute('UPDATE tasks SET deadline_paused_at=? WHERE id=?', (now.isoformat(), task['id']))
        seconds = self.running_elapsed(task, now)
        expires = (now + timedelta(seconds=self.clarification_timeout())).isoformat() if status == 'waiting_clarification' else None
        con.execute('UPDATE tasks SET running_seconds=?,running_since=?,clarification_expires_at=? WHERE id=?',
                    (seconds, now.isoformat() if status == 'running' else None, expires, task['id']))

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA busy_timeout=5000')
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        task = dict(row)
        for key in JSON_FIELDS:
            if key in task:
                task[key] = json.loads(task[key]) if task[key] is not None else None
        return task

    def _get(self, con, task_id):
        return self.decode(con.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())

    def _event(self, con, task, kind, payload=None):
        # Events carry the card view: repeating the whole transcript on every
        # progress step made the events/trace tables grow quadratically.
        payload = payload if payload is not None else {'task': task_card(task)}
        cur = con.execute('INSERT INTO events(task_id,session_id,revision,kind,payload,created_at) VALUES(?,?,?,?,?,?)',
                          (task['id'], task['session_id'], task['revision'], kind,
                           json.dumps(payload, ensure_ascii=False), utcnow()))
        self._append_trace(con, task['session_id'], 'task.' + kind, payload,
                           turn_id=(task.get('context') or {}).get('origin_turn_id') or
                                   (task.get('context') or {}).get('turn_id'),
                           task_id=task['id'], revision=task['revision'],
                           step=task.get('steps'), status=task['status'])
        return cur.lastrowid

    def _append_trace(self, con, session_id, kind, data, *, turn_id=None, task_id=None,
                      revision=None, call_id=None, step=None, status=None):
        # The caller owns this short transaction; no network work occurs here.
        at = (data.get('observed_at') or utcnow()) if isinstance(data, dict) else utcnow()
        encoded = json.dumps(_trace_data(data), ensure_ascii=False, allow_nan=False)
        cur = con.execute('''INSERT INTO trace_events
            (session_id,turn_id,task_id,revision,call_id,step,kind,status,created_at,data)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (session_id, turn_id, task_id, revision, call_id, step, kind, status, at, encoded))
        return {'trace_id': cur.lastrowid, 'session_id': session_id, 'turn_id': turn_id,
                'task_id': task_id, 'revision': revision, 'call_id': call_id, 'step': step,
                'kind': kind, 'status': status, 'created_at': at, 'data': json.loads(encoded)}

    def append_trace(self, session_id, kind, data, *, turn_id=None, task_id=None,
                     revision=None, call_id=None, step=None, status=None):
        with self.connection() as con:
            return self._append_trace(con, session_id, kind, data, turn_id=turn_id,
                                      task_id=task_id, revision=revision, call_id=call_id,
                                      step=step, status=status)

    def traces_after(self, session_id, after_id=0, turn_id=None, task_id=None, limit=200):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError('trace limit 必须为正整数')
        where, args = 'session_id=? AND trace_id>?', [session_id, after_id]
        for key, value in (('turn_id', turn_id), ('task_id', task_id)):
            if value is not None:
                where += f' AND {key}=?'
                args.append(value)
        with self.connection() as con:
            items = []
            for row in con.execute(f'SELECT * FROM trace_events WHERE {where} ORDER BY trace_id LIMIT ?', args + [limit]):
                item = dict(row)
                item['data'] = json.loads(item['data'])
                items.append(item)
            return items

    def create_task(self, session_id, goal, context=None):
        task_id, at = uuid4().hex[:12], utcnow()
        deadline = (datetime.fromisoformat(at) + timedelta(seconds=60)).isoformat()
        with self.connection() as con:
            con.execute('INSERT INTO tasks(id,session_id,goal,context,created_at,updated_at,deadline_at,summary) VALUES(?,?,?,?,?,?,?,?)',
                        (task_id, session_id, goal, json.dumps(context or {}, ensure_ascii=False), at, at, deadline, feedback_text('task.db.queued')))
            task = self._get(con, task_id)
            self._event(con, task, 'created')
            return task

    def get_task(self, task_id):
        with self.connection() as con:
            return self._get(con, task_id)

    def list_tasks(self, session_id):
        with self.connection() as con:
            return [self.decode(row) for row in con.execute('SELECT * FROM tasks WHERE session_id=? ORDER BY created_at', (session_id,))]

    def claim_next(self, exclude_ids=()):
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            exclusion = (' AND id NOT IN (' + ','.join('?' for _ in exclude_ids) + ')') if exclude_ids else ''
            row = con.execute("SELECT * FROM tasks WHERE status IN ('queued','cancelling')" + exclusion + " ORDER BY created_at LIMIT 1", tuple(exclude_ids)).fetchone()
            if row is None:
                return None
            task = self.decode(row)
            if task['status'] == 'queued':
                self._transition_clock(con, task, 'running')
                con.execute("UPDATE tasks SET status='running',summary=?,updated_at=? WHERE id=?",
                            (feedback_text('task.db.running'), utcnow(), task['id']))
                task = self._get(con, task['id'])
                self._event(con, task, 'running')
            return task

    def mutate_task(self, task_id, action, text='', expected_revision=None):
        if action not in {'reply', 'update', 'cancel'}:
            raise ValueError('未知任务操作')
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            task = self._get(con, task_id)
            if task is None:
                raise ValueError('任务不存在')
            if expected_revision is not None and task['revision'] != expected_revision:
                raise ValueError(f"任务版本冲突；当前版本 {task['revision']}，请刷新后重试")
            if task['status'] in {'cancelling', 'cancelled'}:
                raise ValueError('任务正在取消或已取消，请新建任务')
            if action == 'cancel' and task['status'] in TERMINAL:
                raise ValueError('任务已结束')
            if action != 'cancel' and not text.strip():
                raise ValueError('请提供补充内容')
            revision = task['revision'] + 1
            constraints = task['constraints'] or []
            context = task.get('context') or {}
            if action == 'reply':
                clarification = {'question': task.get('question') or feedback_text('task.db.clarification_label'), 'answer': text.strip(), 'revision': revision}
                context.setdefault('clarification_history', []).append(clarification)
            if action != 'cancel':
                constraints.append(text.strip())
            status = 'cancelling' if action == 'cancel' else ('running' if task['status'] == 'running' else 'queued')
            self._transition_clock(con, task, status)
            summary = feedback_text('task.db.cancel_pending') if action == 'cancel' else feedback_text('task.db.update_pending')
            con.execute("UPDATE tasks SET revision=?,constraints=?,context=?,status=?,summary=?,result=NULL,question=NULL,error=NULL,error_code=NULL,progress='{}',updated_at=? WHERE id=?",
                        (revision, json.dumps(constraints, ensure_ascii=False), json.dumps(context, ensure_ascii=False), status, summary, utcnow(), task_id))
            con.execute('INSERT INTO inbox(task_id,action,text,revision,created_at) VALUES(?,?,?,?,?)',
                        (task_id, action, text.strip(), revision, utcnow()))
            task = self._get(con, task_id)
            self._event(con, task, action)
            return task

    def inbox(self, task_id):
        with self.connection() as con:
            rows = [dict(r) for r in con.execute('SELECT * FROM inbox WHERE task_id=? AND consumed=0 ORDER BY id', (task_id,))]
            con.execute('UPDATE inbox SET consumed=1 WHERE task_id=? AND consumed=0', (task_id,))
            return rows

    def apply_pending(self, task_id):
        # Constraints/revision are applied transactionally on receipt; consuming marks the worker boundary.
        with self.connection() as con:
            con.execute('UPDATE inbox SET consumed=1 WHERE task_id=? AND consumed=0', (task_id,))
            return self._get(con, task_id)

    def save_task(self, task_id, revision, **fields):
        allowed = {'status', 'summary', 'result', 'sources', 'transcript', 'question', 'error', 'steps', 'checkpoint', 'progress', 'error_code'}
        if not fields or not set(fields) <= allowed:
            raise ValueError('非法任务字段')
        values = [json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS else v for k, v in fields.items()]
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            task = self._get(con, task_id)
            if not task or task['revision'] != revision or task['status'] in TERMINAL:
                return False
            if task['status'] == 'cancelling' and fields.get('status') != 'cancelled':
                return False
            if 'status' in fields:
                self._transition_clock(con, task, fields['status'])
            assignments = ','.join(f'{key}=?' for key in fields)
            con.execute(f'UPDATE tasks SET {assignments},updated_at=? WHERE id=? AND revision=?',
                        values + [utcnow(), task_id, revision])
            updated = self._get(con, task_id)
            self._event(con, updated, fields.get('status', 'progress'))
            return True

    def event(self, task_id, kind, payload):
        with self.connection() as con:
            task = self._get(con, task_id)
            if not task:
                raise ValueError('任务不存在')
            return self._event(con, task, kind, payload)

    def events_after(self, event_id, session_id=None):
        with self.connection() as con:
            where, args = 'event_id>?', [event_id]
            if session_id:
                where += ' AND session_id=?'
                args.append(session_id)
            items = []
            for row in con.execute(f'SELECT * FROM events WHERE {where} ORDER BY event_id LIMIT 200', args):
                item = dict(row)
                item['payload'] = json.loads(item['payload'])
                items.append(item)
            return items

    def notify_task_progress(self, task_id):
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            task = self._get(con, task_id)
            if (not task or task['status'] not in {'queued', 'running'} or task['progress_notified']
                    or not timedelta(0) < datetime.fromisoformat(task['deadline_at']) - datetime.now(timezone.utc) <= timedelta(seconds=30)):
                return False
            con.execute('UPDATE tasks SET progress_notified=1 WHERE id=?', (task_id,))
            self._event(con, self._get(con, task_id), 'progress_notice')
            return True

    def expire_tasks(self, max_age_seconds=60):
        now, count = datetime.now(timezone.utc), 0
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            tasks = [self.decode(row) for row in con.execute(
                "SELECT * FROM tasks WHERE status IN ('queued','running','waiting_clarification','cancelling')")]
            for task in tasks:
                expired_wait = (task['status'] == 'waiting_clarification' and task.get('clarification_expires_at')
                                and datetime.fromisoformat(task['clarification_expires_at']) <= now)
                exhausted = (self.running_elapsed(task, now) >= max_age_seconds or
                             task['status'] != 'waiting_clarification' and
                             datetime.fromisoformat(task['deadline_at']) <= now)
                cancel_expired = (task['status'] == 'cancelling' and
                                  (now - datetime.fromisoformat(task['updated_at'])).total_seconds() >= max_age_seconds)
                if not expired_wait and not exhausted and not cancel_expired:
                    if (task['status'] in {'queued', 'running'} and not task['progress_notified'] and
                            datetime.fromisoformat(task['deadline_at']) - now <= timedelta(seconds=30)):
                        con.execute('UPDATE tasks SET progress_notified=1 WHERE id=?', (task['id'],))
                        self._event(con, self._get(con, task['id']), 'progress_notice')
                    continue
                status = 'cancelled' if task['status'] == 'cancelling' else 'failed'
                error = None if status == 'cancelled' else ('澄清等待已超时，请新建任务。' if expired_wait
                        else f'任务累计执行达到{max_age_seconds:g}秒上限，已停止。')
                code = None if status == 'cancelled' else 'clarification_timeout' if expired_wait else 'deadline'
                closed = {**task, 'status': status, 'error_code': code}
                summary, result = task_user_detail(closed), partial_result(closed)
                self._transition_clock(con, task, status)
                con.execute('UPDATE tasks SET status=?,error=?,error_code=?,summary=?,result=?,question=NULL,updated_at=? WHERE id=?',
                            (status, error, code, summary, json.dumps(result, ensure_ascii=False) if result else None,
                             now.isoformat(), task['id']))
                self._event(con, self._get(con, task['id']), status)
                count += 1
        return count
