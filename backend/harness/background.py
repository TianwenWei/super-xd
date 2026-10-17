"""Thinker registration and execution against the Gateway's authoritative store."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Callable, Literal

from pydantic import Field

from backend.speaker import speaker_context
from backend.persona import PERSONAS, render
from backend.speech_config import feedback_text
from backend.speech_text import brief_result, speech_text
from backend.task_feedback import task_user_detail
from .contracts import Contract
from .gateway_contracts import PersonaId, TaskStatus
from .release import get_release_id
from .run import RunProtocolError


class TaskExecutionRequest(Contract):
    protocol_version: Literal['gateway-v1'] = 'gateway-v1'
    conversation_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    execution_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=1, max_length=1000)
    release_id: str = Field(min_length=1)
    model_policy_id: str = Field(min_length=1, max_length=200)


class TaskExecutionResult(Contract):
    protocol_version: Literal['gateway-v1'] = 'gateway-v1'
    task: dict[str, Any]
    requested_revision: int = Field(ge=1)
    stale: bool = False
    release_id: str
    model_policy_id: str


class TaskMutationRequest(Contract):
    conversation_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)
    action: Literal['reply', 'update', 'cancel']
    text: str = Field(default='', max_length=4000)
    idempotency_key: str = Field(min_length=1, max_length=128)


class TaskPresentationRequest(Contract):
    conversation_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    expected_status: TaskStatus
    persona_id: PersonaId = 'professional'


class TaskPresentationResult(Contract):
    task_id: str
    revision: int
    status: str
    stale: bool = False
    event_key: str | None = None
    kind: Literal['progress', 'clarify', 'error', 'result'] | None = None
    speech: str = ''
    card: dict[str, Any] | None = None


class TaskService:
    """The implementation owns reasoning; its injected Gateway adapter owns data.

    runner_factory receives a scoped, leased store implementing TaskStore and
    TaskTraceSink. No SQLite worker, polling loop or local task registry is used.
    """

    def __init__(self, store, runner_factory: Callable, *, model_policy_id: str, release_id: str | None = None):
        if not model_policy_id:
            raise ValueError('A deployed Thinker model policy identity is required')
        self.store, self.runner_factory = store, runner_factory
        self.release_id = release_id or get_release_id()
        self.model_policy_id = model_policy_id

    def _versions(self, task):
        context = task.get('context') or {}
        if context.get('release_id') != self.release_id or context.get('model_policy_id') != self.model_policy_id:
            raise RunProtocolError('Task deployment release/model policy does not match its registration')

    async def handle(self, start, request, arguments=None, *, manual_registration=False):
        if not start.conversation_id:
            raise RunProtocolError('Background operations require a Gateway conversation identity')
        store = self.store.for_scope(start.conversation_id)
        arguments = deepcopy(arguments if arguments is not None else request.arguments)
        if request.name == 'delegate_task':
            task_id = 'task-' + hashlib.sha256(request.invocation_id.encode()).hexdigest()[:32]
            history = [{'role': row.role, 'content': row.content}
                       for turn in start.history[-4:] for row in turn.records
                       if row.role in {'user', 'assistant'} and row.content and not getattr(row, 'tool_calls', [])]
            history.append({'role': 'user', 'content': start.user_text})
            context = {'release_id': self.release_id, 'model_policy_id': self.model_policy_id,
                       'origin_identity': None if manual_registration else start.identity.model_dump(),
                       'origin_turn_id': None if manual_registration else start.identity.turn_id,
                       'speaker': speaker_context(start.speaker_seat), 'vehicle': deepcopy(start.context.vehicle.state),
                       'recent_history': history, 'captured_at': start.received_at,
                       'timezone': start.context.environment.timezone,
                       'current_location': start.context.environment.location.model_dump() if start.context.environment.location else None,
                       'query_mode': arguments.get('query_mode', 'research'),
                       'query_domain': arguments.get('query_domain', 'general'),
                       'spoken_goal': arguments.get('spoken_goal'), 'read_details': arguments.get('read_details', False),
                       'required_source_domains': arguments.get('required_source_domains', [])}
            task = await store.create_task(task_id, arguments['goal'], context, request.idempotency_key)
            if task.get('id') != task_id or task.get('revision') != 1 or task.get('status') != 'queued':
                raise RunProtocolError('Task registration returned an unexpected identity/revision')
            self._versions(task)
            return {'ok': True, 'registered': True, 'executed': False, 'task_id': task_id,
                    'revision': task['revision'], 'status': task['status'], 'task': task}
        if request.name != 'manage_task':
            raise RunProtocolError('Unsupported task orchestration request')
        identities = arguments.get('task_ids') or [arguments.get('task_id')]
        if any(not identity for identity in identities):
            raise RunProtocolError('Task management requires resolved task identities')
        if arguments['action'] == 'get':
            tasks = []
            for identity in identities:
                task = await store.read(identity)
                if task is None:
                    return {'ok': False, 'code': 'task_not_found', 'task_id': identity}
                tasks.append(task)
            return {'ok': True, 'tasks': tasks, 'task': tasks[0] if len(tasks) == 1 else None}
        if len(identities) != 1:
            raise RunProtocolError('A task mutation requires exactly one task')
        revision = arguments.get('expected_revision')
        if type(revision) is not int or revision < 1:
            raise RunProtocolError('Task mutation requires a policy-resolved task revision')
        task = await self.mutate(TaskMutationRequest(conversation_id=start.conversation_id, task_id=identities[0],
            expected_revision=revision, action=arguments['action'], text=arguments.get('text', ''),
            idempotency_key=request.idempotency_key))
        return {'ok': True, 'task_id': task['id'], 'revision': task['revision'], 'status': task['status'], 'task': task}

    async def mutate(self, request: TaskMutationRequest):
        request = TaskMutationRequest.model_validate(request.model_dump())
        if request.action in {'reply', 'update'} and not request.text.strip():
            raise RunProtocolError('Task reply/update requires text')
        store = self.store.for_scope(request.conversation_id)
        current = await store.read(request.task_id)
        if current is None:
            raise RunProtocolError('Task not found')
        self._versions(current)
        if current['revision'] != request.expected_revision:
            raise RunProtocolError('Task revision changed')
        result = await store.mutate_task(request.task_id, request.expected_revision, request.action,
                                         request.text, request.idempotency_key)
        self._versions(result)
        statuses = {'cancelling', 'cancelled'} if request.action == 'cancel' else {'queued'}
        if (result.get('id') != request.task_id or result.get('revision') != request.expected_revision + 1
                or result.get('status') not in statuses):
            raise RunProtocolError('Task mutation returned an invalid committed revision/status')
        return result

    async def present(self, request: TaskPresentationRequest) -> TaskPresentationResult:
        request = TaskPresentationRequest.model_validate(request.model_dump())
        store = self.store.for_scope(request.conversation_id)
        task = await store.read(request.task_id)
        if task is None:
            raise RunProtocolError('Task not found')
        self._versions(task)
        response = TaskPresentationResult(task_id=task['id'], revision=task['revision'], status=task['status'])
        if task['revision'] != request.revision or task['status'] != request.expected_status:
            return response.model_copy(update={'stale': True})
        if task['status'] == 'awaiting_confirm':
            raise RunProtocolError('Awaiting-confirmation tasks require an explicit confirmation contract')
        result = task.get('result')
        full = ((result.get('summary') or result.get('answer') or json.dumps(result, ensure_ascii=False))
                if isinstance(result, dict) else result) or task.get('summary') or ''
        selected = (str(full) if task.get('context', {}).get('read_details') is True else
                    ((result.get('spoken_summary') if isinstance(result, dict) else None) or brief_result(str(full))))
        detail = selected if task['status'] == 'completed' else task_user_detail(task)
        if task['status'] == 'completed' and isinstance(result, dict) and result.get('partial') is True:
            detail = feedback_text('task.partial_prefix') + detail
        kind = ('progress' if task['status'] in {'queued', 'running'} else
                'clarify' if task['status'] == 'waiting_clarification' else
                'error' if task['status'] == 'failed' else 'result')
        card = {'task_id': task['id'], 'revision': task['revision'], 'status': task['status'],
                'goal': task['goal'], 'result': deepcopy(result), 'sources': deepcopy(task.get('sources', [])),
                'suggestions_require_new_run': True}
        payload = {'task_id': task['id'], 'revision': task['revision'], 'status': task['status'],
                   'steps': task.get('steps', 0), 'kind': kind, 'detail': detail}
        key = 'task-presentation-' + hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        response = response.model_copy(update={'event_key': key, 'kind': kind,
            'speech': render(PERSONAS[request.persona_id], kind, speech_text(str(detail))), 'card': card})
        await store.emit(request.conversation_id, 'task.presentation', response.model_dump(), task_id=task['id'])
        return response

    async def execute(self, request: TaskExecutionRequest) -> TaskExecutionResult:
        request = TaskExecutionRequest.model_validate(request.model_dump())
        if request.release_id != self.release_id or request.model_policy_id != self.model_policy_id:
            raise RunProtocolError('Task execution deployment does not match')
        store = self.store.for_scope(request.conversation_id, execution_id=request.execution_id,
                                    lease_token=request.lease_token)
        task = await store.refresh(request.task_id)
        if task is None:
            raise RunProtocolError('Task not found')
        self._versions(task)
        if task['revision'] != request.revision:
            return self._result(task, request, stale=True)
        if task['status'] in {'completed', 'failed', 'cancelled', 'waiting_clarification', 'awaiting_confirm'}:
            return self._result(task, request)
        if task['status'] == 'queued':
            accepted = await store.save(task['id'], request.revision, status='running',
                                        running_since=datetime.now(timezone.utc).isoformat())
            if not accepted:
                return self._result(await store.read(task['id']) or task, request, stale=True)
            task = await store.read(task['id'])
        runner = self.runner_factory(store)
        await runner.run_task(task)
        current = await store.read(task['id'])
        if current is None:
            raise RunProtocolError('Task disappeared during execution')
        return self._result(current, request, stale=current['revision'] != request.revision)

    def _result(self, task, request, *, stale=False):
        return TaskExecutionResult(task=task, requested_revision=request.revision, stale=stale,
                                   release_id=self.release_id, model_policy_id=self.model_policy_id)
