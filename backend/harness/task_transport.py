"""Async client for Gateway-owned task state; no local database or scheduler."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import math
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import ConfigDict, Field, field_validator

from backend.services.config import validate_url
from .contracts import Contract
from .diagnostics import safe_diagnostic


class GatewayTransportError(RuntimeError):
    def __init__(self, code, *, status_code=None, uncertain=False):
        super().__init__(code)
        self.code, self.status_code, self.uncertain = code, status_code, uncertain


class GatewayConflict(GatewayTransportError):
    pass


def _aware(value):
    if value is not None:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError('Task timestamps require an explicit UTC offset')
    return value


class TaskSnapshot(Contract):
    # The ledger can retain additional UI/audit fields. The execution fields
    # below are validated before they enter TaskRunner, and all extras are JSON.
    model_config = ConfigDict(extra='allow', strict=True)
    id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=4000)
    revision: int = Field(ge=1)
    status: Literal['queued', 'running', 'waiting_clarification', 'awaiting_confirm',
                    'cancelling', 'completed', 'failed', 'cancelled']
    deadline_at: str
    running_seconds: float = Field(default=0.0, ge=0)
    running_since: str | None = None
    constraints: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    steps: int = Field(default=0, ge=0)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    _timestamp = field_validator('deadline_at', 'running_since')(_aware)


class TaskResponse(Contract):
    task: TaskSnapshot | None


class AppliedResponse(Contract):
    applied: bool


class Acknowledgement(Contract):
    accepted: Literal[True]


class UsageResponse(Contract):
    execution_seconds: float = Field(ge=0)
    lookup_count: int = Field(ge=0)


def _identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or value in {'.', '..'}:
        raise ValueError('A nonempty opaque ID of at most 128 characters is required')
    return quote(value, safe='')


class GatewayTaskStore:
    """The remote API atomically owns state/events, CAS and execution leases.

    This adapter never retries a write after an ambiguous network failure.
    create/mutate carry a stable idempotency key for explicit caller retries.
    """

    def __init__(self, base_url, *, token='', client=None, timeout_seconds=10, secrets=(),
                 max_response_bytes=8 * 1024 * 1024):
        self.base_url = validate_url(base_url)
        if not self.base_url:
            raise ValueError('NLP_GATEWAY_URL is required for the task ledger')
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError('Gateway timeout must be positive and finite')
        if type(max_response_bytes) is not int or max_response_bytes < 1:
            raise ValueError('Gateway response size limit must be positive')
        self.token, self.secrets = token, tuple(secrets) + (token,)
        self.timeout_seconds, self.max_response_bytes = timeout_seconds, max_response_bytes
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)
        self.owns_client = client is None

    def for_scope(self, conversation_id, *, execution_id=None, lease_token=None):
        _identifier(conversation_id)
        if (execution_id is None) != (lease_token is None):
            raise ValueError('Execution identity and lease token must be supplied together')
        if execution_id is not None:
            _identifier(execution_id)
            if not isinstance(lease_token, str) or not lease_token.strip():
                raise ValueError('An execution lease token is required')
        return BoundTaskStore(self, conversation_id, execution_id, lease_token)

    async def request(self, path, payload, response_type):
        # Validate before any network effect; errors never echo request payloads.
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        try:
            async with self.client.stream('POST', self.base_url + path, content=encoded, headers=headers,
                                          timeout=self.timeout_seconds, follow_redirects=False) as response:
                if response.status_code == 409:
                    raise GatewayConflict('gateway_revision_or_lease_conflict', status_code=409)
                if response.status_code >= 500:
                    raise GatewayTransportError('gateway_unavailable', status_code=response.status_code, uncertain=True)
                if response.status_code != 200:
                    raise GatewayTransportError('gateway_request_rejected', status_code=response.status_code)
                pieces, length = [], 0
                async for piece in response.aiter_bytes():
                    length += len(piece)
                    if length > self.max_response_bytes:
                        raise GatewayTransportError('gateway_response_too_large', uncertain=True)
                    pieces.append(piece)
                try:
                    return response_type.model_validate_json(b''.join(pieces))
                except (ValueError, TypeError):
                    raise GatewayTransportError('gateway_invalid_response', uncertain=True) from None
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            raise GatewayTransportError('gateway_transport_failure', uncertain=True) from None

    async def aclose(self):
        if self.owns_client:
            await self.client.aclose()


class BoundTaskStore:
    def __init__(self, owner, conversation_id, execution_id, lease_token):
        self.owner, self.conversation_id = owner, conversation_id
        self.execution_id, self.lease_token = execution_id, lease_token
        self.base = '/internal/nlp/conversations/' + _identifier(conversation_id)

    def _scope(self, *, executing=False):
        if executing and self.lease_token is None:
            raise ValueError('Task execution writes require a Gateway execution lease')
        return {'conversation_id': self.conversation_id, 'execution_id': self.execution_id,
                'lease_token': self.lease_token}

    async def _task(self, task_id, operation, fields=None, *, required=False):
        result = await self.owner.request(self.base + '/tasks/' + _identifier(task_id) + '/' + operation,
                                          {**self._scope(), **(fields or {})}, TaskResponse)
        task = result.task
        if task is None:
            if required:
                raise GatewayTransportError('gateway_missing_task', uncertain=True)
            return None
        if task.id != task_id or task.session_id != self.conversation_id:
            raise GatewayTransportError('gateway_task_scope_mismatch', uncertain=True)
        return task.model_dump()

    async def read(self, task_id):
        return await self._task(task_id, 'read')

    async def refresh(self, task_id):
        return await self._task(task_id, 'refresh')

    async def create_task(self, task_id, goal, context, idempotency_key):
        _identifier(idempotency_key)
        return await self._task(task_id, 'create', {'goal': goal, 'context': deepcopy(context),
                                'idempotency_key': idempotency_key}, required=True)

    async def mutate_task(self, task_id, expected_revision, action, text, idempotency_key):
        _identifier(idempotency_key)
        if action not in {'reply', 'update', 'cancel'}:
            raise ValueError('Unsupported task mutation')
        return await self._task(task_id, 'mutate', {'expected_revision': expected_revision,
                                'action': action, 'text': text, 'idempotency_key': idempotency_key}, required=True)

    async def save(self, task_id, revision, **fields):
        try:
            result = await self.owner.request(self.base + '/tasks/' + _identifier(task_id) + '/save',
                {**self._scope(executing=True), 'expected_revision': revision, 'fields': fields}, AppliedResponse)
        except GatewayConflict:
            return False
        return result.applied

    async def expire(self, task_id, max_age_seconds):
        await self.owner.request(self.base + '/tasks/' + _identifier(task_id) + '/expire',
            {**self._scope(executing=True), 'max_age_seconds': max_age_seconds}, Acknowledgement)

    async def notify_progress(self, task_id):
        result = await self.owner.request(self.base + '/tasks/' + _identifier(task_id) + '/notify-progress',
                                          self._scope(executing=True), AppliedResponse)
        return result.applied

    async def usage(self, task_id, session_id):
        if session_id != self.conversation_id:
            raise ValueError('Usage scope does not match the task conversation')
        result = await self.owner.request(self.base + '/tasks/' + _identifier(task_id) + '/usage',
                                          self._scope(), UsageResponse)
        return result.execution_seconds, result.lookup_count

    async def emit(self, session_id, kind, data, **fields):
        if session_id != self.conversation_id:
            raise ValueError('Trace scope does not match the task conversation')
        record = {'kind': kind, 'data': data, **fields}
        safe = safe_diagnostic(record, self.owner.secrets + (self.lease_token,))
        await self.owner.request(self.base + '/traces', {**self._scope(), 'record': safe}, Acknowledgement)
        return safe
