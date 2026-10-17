"""Gateway V1: factual inputs, explicit policy, and resumable NLP handoffs.

This module supplies no Gateway, transport, storage, device executor or default
semantic authorization policy. The application supplies its model and policy.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
import asyncio
from datetime import datetime
import json
import time
from typing import Annotated, Any, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio
from pydantic import Field, field_validator, model_validator

from backend.persona import PERSONAS, style_prompt
from backend.model_context import compact_message
from backend.prompt_templates import render_prompt
from backend.speaker import speaker_context

from .context import ContextTurn
from .contracts import (Contract, PreparedRun, RunBudget, RunCheckpoint, RunIdentity,
                        RunEvent, ToolObservation, ToolRequest)
from .run import AgentRun, ModelStream, RunProtocolError
from .release import get_release_id
from .talker import prepare_talker_run
from .foreground import ForegroundGroup, GroupCheckpoint, OrchestrationCheckpoint


GATEWAY_VERSION = 'gateway-v1'
Seat = Literal['driver', 'front_passenger', 'rear_left', 'rear_right']
PersonaId = Literal['professional', 'warm', 'playful']


def _timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('Timestamps require an explicit UTC offset')
    return value


class VersionedSnapshot(Contract):
    revision: int = Field(ge=0)
    observed_at: str

    _observed_at = field_validator('observed_at')(_timestamp)


class VehicleSnapshot(VersionedSnapshot):
    state: dict[str, dict[str, Any]]


class ToolCapability(Contract):
    name: str = Field(min_length=1, max_length=128)
    operation: Literal['query', 'control']
    execution_location: Literal['cloud', 'device']
    domain: str = Field(min_length=1, max_length=64)
    action: str | None = None
    content_kind: str | None = None
    available: bool = True


class CapabilitySnapshot(VersionedSnapshot):
    tools: list[ToolCapability] = Field(default_factory=list, max_length=128)
    features: dict[str, bool] = Field(default_factory=dict)
    cockpit: dict[str, Any] = Field(default_factory=dict)


class LocationFact(Contract):
    name: str = Field(min_length=1, max_length=200)
    country: str = Field(default='', max_length=200)
    source: Literal['user_setting', 'user_utterance', 'device', 'gps']


class EnvironmentFacts(Contract):
    timezone: str
    location: LocationFact | None = None

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError('An IANA timezone is required') from error
        return value


class PlaceReference(Contract):
    place_ref: str = Field(min_length=1, max_length=300)
    name: str = Field(min_length=1, max_length=300)
    provider: str = Field(min_length=1, max_length=128)
    source_turn_id: str = Field(min_length=1, max_length=128)
    presented_to_user: bool
    selected_by_user: bool = False
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def selected_requires_presentation(self):
        if self.selected_by_user and not self.presented_to_user:
            raise ValueError('A selected place must have been presented to the user')
        return self


class MediaReference(Contract):
    content_id: str = Field(min_length=1, max_length=500)
    kind: Literal['music', 'video', 'radio']
    title: str = Field(min_length=1, max_length=500)
    provider: str = Field(min_length=1, max_length=128)
    source_turn_id: str = Field(min_length=1, max_length=128)
    presented_to_user: bool


TaskStatus = Literal['queued', 'running', 'waiting_clarification', 'cancelling',
                     'completed', 'failed', 'cancelled', 'awaiting_confirm']


class TaskReference(Contract):
    id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    status: TaskStatus
    goal: str = Field(min_length=1, max_length=4000)
    summary: str = ''
    question: str | None = None
    error_code: str | None = None
    result: dict[str, Any] | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class ContextSnapshot(VersionedSnapshot):
    vehicle: VehicleSnapshot
    capabilities: CapabilitySnapshot
    environment: EnvironmentFacts
    cockpit: dict[str, Any] = Field(default_factory=dict)
    tasks: list[TaskReference] = Field(default_factory=list, max_length=100)
    places: list[PlaceReference] = Field(default_factory=list, max_length=100)
    media: list[MediaReference] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def unambiguous_references(self):
        for items, field in ((self.tasks, 'id'), (self.places, 'place_ref'), (self.media, 'content_id')):
            identities = [getattr(item, field) for item in items]
            if len(set(identities)) != len(identities):
                raise ValueError('Context reference identities must be unique')
        return self


class HistoricalCall(Contract):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any]


class UserRecord(Contract):
    role: Literal['user']
    content: str = Field(min_length=1, max_length=16000)


class AssistantRecord(Contract):
    role: Literal['assistant']
    content: str | None = None
    tool_calls: list[HistoricalCall] = Field(default_factory=list, max_length=32)


class ToolRecord(Contract):
    role: Literal['tool']
    tool_call_id: str = Field(min_length=1, max_length=128)
    result: dict[str, Any]


HistoryRecord = Annotated[UserRecord | AssistantRecord | ToolRecord, Field(discriminator='role')]


class HistoryTurn(Contract):
    turn_id: str = Field(min_length=1, max_length=128)
    speaker_seat: Seat | None = None
    records: list[HistoryRecord] = Field(min_length=1, max_length=100)
    proactive: bool = False
    source_task: TaskReference | None = None
    pending_tool_group: bool = False
    interrupted_playback: bool = False
    heard_text: str = ''

    @model_validator(mode='after')
    def valid_protocol(self):
        if self.proactive != (self.source_task is not None):
            raise ValueError('Proactive history requires structured task attribution')
        pending = set()
        for record in self.records:
            if isinstance(record, ToolRecord):
                if record.tool_call_id not in pending:
                    raise ValueError('Historical tool result has no pending call')
                pending.remove(record.tool_call_id)
            else:
                if pending:
                    raise ValueError('A historical tool group must close before the next message')
                if isinstance(record, AssistantRecord):
                    pending = {call.id for call in record.tool_calls}
                    if len(pending) != len(record.tool_calls):
                        raise ValueError('Historical tool call IDs must be unique within a group')
        if bool(pending) != self.pending_tool_group:
            raise ValueError('pending_tool_group must describe the actual historical group')
        return self

    def context_turn(self) -> ContextTurn:
        records = []
        if self.source_task is not None:
            attribution = self.source_task.model_dump(include={'id', 'goal', 'revision', 'status'})
            records.append({'role': 'system', 'content': render_prompt('history_attribution',
                           attribution_json=json.dumps(attribution, ensure_ascii=False))})
        for item in self.records:
            if isinstance(item, ToolRecord):
                records.append({'role': 'tool', 'tool_call_id': item.tool_call_id,
                                'content': json.dumps(item.result, ensure_ascii=False)})
            elif isinstance(item, AssistantRecord):
                record = {'role': 'assistant', 'content': item.content}
                if item.tool_calls:
                    record['tool_calls'] = [{'id': call.id, 'type': 'function', 'function': {
                        'name': call.name, 'arguments': json.dumps(call.arguments, ensure_ascii=False)}}
                        for call in item.tool_calls]
                records.append(record)
            else:
                records.append(item.model_dump())
        return ContextTurn(self.turn_id, records, proactive=self.proactive,
                           speaker_seat=self.speaker_seat, pending_tool_group=self.pending_tool_group,
                           interrupted_playback=self.interrupted_playback, heard_text=self.heard_text)


class StartRequest(Contract):
    protocol_version: Literal['gateway-v1'] = GATEWAY_VERSION
    identity: RunIdentity
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    received_at: str
    user_text: str = Field(min_length=1, max_length=4000)
    speaker_seat: Seat
    persona_id: PersonaId
    history: list[HistoryTurn] = Field(default_factory=list, max_length=100)
    context: ContextSnapshot
    budget: RunBudget = Field(default_factory=RunBudget)

    _received_at = field_validator('received_at')(_timestamp)

    @model_validator(mode='after')
    def consistent_context(self):
        if not self.user_text.strip():
            raise ValueError('User input cannot be blank')
        if self.identity.context_revision != self.context.revision:
            raise ValueError('Run identity and context snapshot revisions must match')
        identities = [turn.turn_id for turn in self.history]
        if len(set(identities)) != len(identities) or self.identity.turn_id in identities:
            raise ValueError('History must contain distinct prior turns, not the current turn')
        return self


class NLPDispatch(Contract):
    kind: Literal['nlp_orchestration']
    target: Literal['vehicle_planner', 'thinker_task', 'task_management']
    execution_arguments: dict[str, Any] | None = None


class ToolDispatch(Contract):
    kind: Literal['tool_execution']
    operation: Literal['query', 'control']
    execution_location: Literal['cloud', 'device']
    execution_arguments: dict[str, Any] | None = None
    domain: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    prepared_command: dict[str, Any] | None = None


DispatchTarget = Annotated[NLPDispatch | ToolDispatch, Field(discriminator='kind')]
NLP_TARGETS = {'delegate_vehicle_control': 'vehicle_planner',
               'delegate_task': 'thinker_task', 'manage_task': 'task_management'}


class DispatchRequest(Contract):
    request: ToolRequest
    dispatch: DispatchTarget

    @model_validator(mode='after')
    def correct_ownership(self):
        expected = NLP_TARGETS.get(self.request.name)
        if expected is not None:
            if not isinstance(self.dispatch, NLPDispatch) or self.dispatch.target != expected:
                raise ValueError('NLP delegation cannot be routed as a device or cloud action')
        elif not isinstance(self.dispatch, ToolDispatch):
            raise ValueError('A resource/device tool cannot be relabelled as NLP orchestration')
        return self


class GatewayCheckpoint(Contract):
    protocol_version: Literal['gateway-v1'] = GATEWAY_VERSION
    start: StartRequest
    run: RunCheckpoint
    semantic_policy_id: str = Field(min_length=1, max_length=200)
    model_policy_id: str = Field(min_length=1, max_length=200)
    orchestration: OrchestrationCheckpoint = Field(default_factory=OrchestrationCheckpoint)

    @model_validator(mode='after')
    def same_identity(self):
        if self.start.identity != self.run.prepared.identity:
            raise ValueError('Checkpoint belongs to a different run/context revision')
        return self


class ResumeRequest(Contract):
    protocol_version: Literal['gateway-v1'] = GATEWAY_VERSION
    identity: RunIdentity
    checkpoint: GatewayCheckpoint
    observations: list[ToolObservation] = Field(default_factory=list, max_length=128)
    cancel: bool = False

    @model_validator(mode='after')
    def same_run(self):
        if self.identity != self.checkpoint.start.identity:
            raise ValueError('A continuation cannot change its run identity')
        if any(item.identity != self.identity for item in self.observations):
            raise ValueError('Observations must belong to the same run/context revision')
        if self.cancel and self.observations:
            raise ValueError('A cancellation request cannot also submit tool observations')
        return self


class GatewayOutput(Contract):
    protocol_version: Literal['gateway-v1'] = GATEWAY_VERSION
    identity: RunIdentity
    sequence: int = Field(ge=1)
    type: Literal['text.delta', 'run.suspended', 'run.completed', 'run.failed', 'run.cancelled']
    text: str | None = None
    requests: list[DispatchRequest] = Field(default_factory=list)
    checkpoint: GatewayCheckpoint | None = None
    error_code: str | None = None

    @model_validator(mode='after')
    def coherent_handoff(self):
        if self.type == 'text.delta':
            if self.text is None or self.requests or self.checkpoint is not None:
                raise ValueError('A text delta contains text only')
        elif self.checkpoint is None:
            raise ValueError('Every suspension or terminal event carries a checkpoint')
        if self.checkpoint is not None and self.checkpoint.start.identity != self.identity:
            raise ValueError('Output identity must match its checkpoint')
        if self.requests and self.type != 'run.suspended':
            raise ValueError('Only a suspension may hand off new tool requests')
        if self.checkpoint is not None:
            state = self.checkpoint.run
            if self.sequence != state.sequence:
                raise ValueError('Output and checkpoint sequence must match')
            statuses = {'run.suspended': {'waiting_tools', 'waiting_reconciliation'},
                        'run.completed': {'completed'}, 'run.failed': {'failed'}, 'run.cancelled': {'cancelled'}}
            if state.status not in statuses[self.type]:
                raise ValueError('Output and checkpoint status must match')
            groups = self.checkpoint.orchestration.groups
            expected = ([ToolRequest.model_validate(item['request']) for item in groups[-1].external_pending]
                        if groups and not groups[-1].done else state.pending)
            if self.requests and [item.request for item in self.requests] != expected:
                raise ValueError('A handoff must contain its whole pending group')
        return self


class PolicyInput(Contract):
    start: StartRequest
    checkpoint: RunCheckpoint
    requests: list[ToolRequest]


class SemanticPolicy(Protocol):
    """Application-owned, synchronous, side-effect-free whole-group validation.

    Must enforce grounding, POI selection, task references, layer conflicts and
    current capabilities. Return one dispatch target per request in order, or
    raise. A permissive test policy is not a production implementation.
    """
    policy_id: str

    def validate_group(self, input_: PolicyInput) -> Sequence[NLPDispatch | ToolDispatch]: ...


class DiagnosticRecord(Contract):
    kind: Literal['run.event', 'observations.received', 'orchestration.event']
    identity: RunIdentity
    event: RunEvent | None = None
    observations: list[ToolObservation] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def coherent_record(self):
        if self.kind == 'run.event':
            if self.event is None or self.event.identity != self.identity or self.observations:
                raise ValueError('Run diagnostics require a matching event')
        elif self.event is not None or any(item.identity != self.identity for item in self.observations):
            raise ValueError('Observation diagnostics require matching observations only')
        return self


class DiagnosticSink(Protocol):
    """NLP composition root supplies redaction and persistence, never the caller.

    Apply the existing services.contracts.redact path with deployment secrets
    before persisting. Records contain full model context and tool receipts and
    are never part of the user/display stream. A received observation is not yet
    accepted: the following run event records validation and resulting state.
    """

    async def emit(self, record: DiagnosticRecord) -> None: ...


class _DiagnosticsUnavailable(RuntimeError):
    pass


class NLPService:
    def __init__(self, model: ModelStream, *, model_policy_id: str, semantic_policy: SemanticPolicy,
                 release_id: str | None = None, diagnostics: DiagnosticSink | None = None,
                 planner=None, task_service=None, receipt_fast_path: bool = True):
        release_id = get_release_id() if release_id is None else release_id
        identities = (release_id, model_policy_id, getattr(semantic_policy, 'policy_id', None))
        if any(not isinstance(value, str) or not value.strip() for value in identities) or not callable(getattr(semantic_policy, 'validate_group', None)):
            raise ValueError('Application/model releases and an explicit semantic policy are required')
        if len(model_policy_id) > 200 or len(semantic_policy.policy_id) > 200:
            raise ValueError('Deployment policy identities must be at most 200 characters')
        self.model, self.release_id, self.semantic_policy = model, release_id, semantic_policy
        self.model_policy_id = model_policy_id
        self.diagnostics = diagnostics
        self.planner, self.task_service = planner, task_service
        self.receipt_fast_path = receipt_fast_path

    async def execute_task(self, request):
        if self.task_service is None:
            raise RunProtocolError('Task service is not configured')
        return await self.task_service.execute(request)

    async def mutate_task(self, request):
        if self.task_service is None:
            raise RunProtocolError('Task service is not configured')
        return await self.task_service.mutate(request)

    async def present_task(self, request):
        if self.task_service is None:
            raise RunProtocolError('Task service is not configured')
        return await self.task_service.present(request)

    def compile_start(self, request: StartRequest) -> PreparedRun:
        request = StartRequest.model_validate(request.model_dump())
        context = request.context
        environment = context.environment
        vehicle = context.vehicle.model_dump()['state']
        meta = vehicle.setdefault('_meta', {})
        meta.update(timezone=environment.timezone,
                    location=environment.location.name if environment.location else '',
                    location_country=environment.location.country if environment.location else '')
        local = datetime.fromisoformat(request.received_at).astimezone(ZoneInfo(environment.timezone))
        snapshot = {'vehicle': vehicle,
                    'vehicle_state_revision': context.vehicle.revision,
                    'vehicle_observed_at': context.vehicle.observed_at,
                    'context_revision': context.revision, 'context_observed_at': context.observed_at,
                    'capability_revision': context.capabilities.revision,
                    'capabilities_observed_at': context.capabilities.observed_at,
                    'background_tasks_reference_only': [item.model_dump() for item in context.tasks],
                    'speaker': speaker_context(request.speaker_seat),
                    'recent_places': [item.model_dump() for item in context.places],
                    'media_choices': [item.model_dump() for item in context.media],
                    'cockpit': context.cockpit,
                    'cockpit_capabilities': context.capabilities.cockpit,
                    'tool_capabilities': [item.model_dump() for item in context.capabilities.tools],
                    'capabilities': context.capabilities.features,
                    'current_location': environment.location.model_dump() if environment.location else None,
                    'current_utc': request.received_at, 'current_local': local.isoformat(timespec='seconds'),
                    'current_date': local.date().isoformat(), 'timezone': environment.timezone}
        current = ContextTurn(request.identity.turn_id, [{'role': 'user', 'content': request.user_text}],
                              speaker_seat=request.speaker_seat)
        return prepare_talker_run(request.identity, current_turn=current,
                                  turns=[*(turn.context_turn() for turn in request.history), current],
                                  snapshot=snapshot, persona_prompt=style_prompt(PERSONAS[request.persona_id]),
                                  release_id=self.release_id, budget=request.budget)

    def start(self, request: StartRequest) -> GatewayRun:
        return GatewayRun(self, request, self.compile_start(request))

    def resume(self, request: ResumeRequest) -> GatewayRun:
        request = ResumeRequest.model_validate(request.model_dump())
        checkpoint = request.checkpoint
        if checkpoint.run.status in {'completed', 'failed', 'cancelled'}:
            raise RunProtocolError('A terminal run cannot be resumed')
        if checkpoint.semantic_policy_id != self.semantic_policy.policy_id:
            raise RunProtocolError('Continuation requires the original semantic policy release')
        if checkpoint.model_policy_id != self.model_policy_id:
            raise RunProtocolError('Continuation requires the original deployed model policy')
        prepared = self.compile_start(checkpoint.start)
        return GatewayRun(self, checkpoint.start, prepared, checkpoint=checkpoint.run,
                          observations=request.observations, cancel=request.cancel,
                          orchestration=checkpoint.orchestration)


class GatewayRun:
    """NLP-owned child orchestration with caller-owned durable checkpoints."""

    def _model_messages(self, messages):
        # The raw transcript remains in the checkpoint for product receipt
        # replay and audit. Only the actual model request drops task internals.
        prefix = len(self._run.prepared.messages)
        context = {}
        for record in messages[prefix:]:
            for call in record.get('tool_calls', []):
                function = call.get('function', {})
                arguments = json.loads(function.get('arguments', '{}'))
                context[call['id']] = (function.get('name'), arguments.get('read_details') is True)
        result = messages[:prefix]
        for record in messages[prefix:]:
            compact = compact_message(record, *context.get(record.get('tool_call_id'), (None, False)))
            if record.get('role') == 'tool':
                original = json.loads(record['content'])
                if isinstance(original, dict) and 'state' in original:
                    # Unlike the demo actor, a resumed run does not rebuild its
                    # original snapshot. Its current query facts stay visible.
                    body = json.loads(compact['content'])
                    body['state'] = original['state']
                    compact['content'] = json.dumps(body, ensure_ascii=False, separators=(',', ':'))
            result.append(compact)
        return result

    def __init__(self, service, start, prepared, *, checkpoint=None, observations=(), cancel=False,
                 orchestration=None):
        self.service = service
        self.start = StartRequest.model_validate(start.model_dump())
        self.policy = service.semantic_policy
        self.model_policy_id = service.model_policy_id
        self.diagnostics = service.diagnostics
        self._diagnostic_pending = []
        self._diagnostic_bytes = 0
        self._diagnostic_failed = False
        self._dispatches = {}
        self._observations = list(observations)
        self._run = AgentRun(prepared, service.model, checkpoint=checkpoint, validate_group=self._validate_group,
                             prepare_messages=self._model_messages)
        self.orchestration = OrchestrationCheckpoint.model_validate(
            orchestration.model_dump() if orchestration is not None else {})
        self._child = None
        self._active = False
        self._validate_orchestration()
        if cancel:
            self._run.cancel()

    def _validate_orchestration(self):
        state, budget = self._run.state, self._run.prepared.budget
        if state.model_calls + self.orchestration.model_calls > budget.max_model_calls:
            raise RunProtocolError('Child model calls exceed the original budget')
        if state.tool_calls + self.orchestration.tool_calls > budget.max_tool_calls:
            raise RunProtocolError('Child tool calls exceed the original budget')
        for group in self.orchestration.groups:
            ForegroundGroup(self, group)
            for item in group.requests:
                DispatchRequest.model_validate(item)
            for item in group.external_pending:
                DispatchRequest.model_validate(item)
            if any(item.identity != self.start.identity for item in [*group.receipts.values(), *group.results.values()]):
                raise RunProtocolError('Child receipts belong to another run')
        if self.orchestration.groups and not self.orchestration.groups[-1].done:
            group = self.orchestration.groups[-1]
            if [ToolRequest.model_validate(item['request']) for item in group.requests] != state.pending:
                raise RunProtocolError('Orchestration group differs from the original model calls')

    def _validate_group(self, requests):
        input_ = PolicyInput(start=self.start.model_copy(deep=True),
                             checkpoint=self._run.state.model_copy(deep=True),
                             requests=[item.model_copy(deep=True) for item in requests])
        targets = list(self.policy.validate_group(input_))
        if len(targets) != len(requests):
            raise RunProtocolError('Semantic policy must decide the entire tool group')
        checked = [DispatchRequest(request=request, dispatch=target) for request, target in zip(requests, targets)]
        self._dispatches = {item.request.invocation_id: item for item in checked}

    def cancel(self):
        self._run.cancel()
        if self._child is not None:
            self._child.cancel()

    def _checkpoint(self, state):
        return GatewayCheckpoint(start=self.start.model_copy(deep=True), run=state,
                                 semantic_policy_id=self.policy.policy_id, model_policy_id=self.model_policy_id,
                                 orchestration=self.orchestration.model_copy(deep=True))

    def checkpoint(self) -> GatewayCheckpoint:
        return self._checkpoint(self._run.checkpoint())

    async def _diagnostic(self, record):
        if self.diagnostics is None:
            return
        if self._diagnostic_failed:
            raise _DiagnosticsUnavailable
        self._diagnostic_pending.append(record)
        delta = record.kind == 'run.event' and record.event.type == 'text.delta'
        if delta:
            self._diagnostic_bytes += len(record.event.data['text'].encode())
            if len(self._diagnostic_pending) < 64 and self._diagnostic_bytes < 32768:
                return
        await self._flush_diagnostics()

    async def _flush_diagnostics(self):
        if not self._diagnostic_pending:
            return
        records, self._diagnostic_pending = self._diagnostic_pending, []
        self._diagnostic_bytes = 0

        async def persist():
            # A stalled sink cannot hold cancellation or connection cleanup forever.
            async with asyncio.timeout(10):
                emit_many = getattr(self.diagnostics, 'emit_many', None)
                if callable(emit_many):
                    await emit_many(records)
                else:
                    for record in records:
                        await self.diagnostics.emit(record)

        write = asyncio.create_task(persist())
        cancelled = False
        # Finish an in-flight commit before propagating disconnect cancellation:
        # dropping its ACK would make an automatic resend ambiguous.
        with anyio.CancelScope(shield=True):
            while not write.done():
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
        try:
            write.result()
        except Exception as error:
            self._diagnostic_failed = True
            raise _DiagnosticsUnavailable from error
        if cancelled:
            raise asyncio.CancelledError

    def _diagnostic_failure(self):
        self._run.state.status = 'failed'
        self._run.state.error_code = 'diagnostics_unavailable'
        return self._run._event('run.failed', code='diagnostics_unavailable')

    def _check_cancelled(self):
        if self._run._cancel.is_set():
            raise asyncio.CancelledError

    async def _child_call(self, coroutine):
        self._child = asyncio.create_task(coroutine)
        try:
            return await self._child
        finally:
            self._child = None

    def _history(self):
        return [{'role': item.role, 'content': item.content} for turn in self.start.history[-4:]
                for item in turn.records if item.role in {'user', 'assistant'} and item.content]

    def _reserve_child_tool(self):
        if self._run.state.tool_calls + self.orchestration.tool_calls >= self.start.budget.max_tool_calls:
            raise RunProtocolError('tool_call_budget_exhausted')
        self.orchestration.tool_calls += 1

    async def _plan(self, request, vehicle):
        records = []
        def trace(kind, data, **fields):
            if kind == 'model.started':
                if self._run.state.model_calls + self.orchestration.model_calls >= self.start.budget.max_model_calls:
                    raise RunProtocolError('model_call_budget_exhausted')
                self.orchestration.model_calls += 1
            records.append({'kind': kind, 'data': data, **fields})
        remaining = self.start.budget.active_timeout_seconds - self._run.state.active_seconds
        if remaining <= 0:
            raise RunProtocolError('active_time_budget_exhausted')
        started = time.monotonic()
        try:
            async with asyncio.timeout(remaining):
                return await self._child_call(self.service.planner.plan(self.start.user_text, vehicle,
                    request.arguments['skill_ids'], history=self._history(), speaker_seat=self.start.speaker_seat,
                    capabilities=self.start.context.capabilities.features, trace=trace))
        finally:
            elapsed = time.monotonic() - started
            self.orchestration.active_seconds += elapsed
            self._run.state.active_seconds += elapsed
            if self.diagnostics is not None:
                self._diagnostic_pending.extend(DiagnosticRecord(kind='orchestration.event',
                    identity=self.start.identity, data=record) for record in records)
                await self._flush_diagnostics()

    async def _suspend_group(self, requests):
        group = self.orchestration.groups[-1]
        unknown = any(group.receipts.get(item['request']['invocation_id']) is not None and
                      group.receipts[item['request']['invocation_id']].status == 'unknown'
                      for item in group.external_pending)
        self._run.state.status = 'waiting_reconciliation' if unknown else 'waiting_tools'
        event = self._run._event('run.suspended', requests=requests, status=self._run.state.status)
        output = GatewayOutput(identity=event.identity, sequence=event.sequence, type='run.suspended',
            requests=[DispatchRequest.model_validate(item) for item in requests], checkpoint=self.checkpoint())
        event.data['checkpoint'] = output.checkpoint.model_dump()
        await self._diagnostic(DiagnosticRecord(kind='run.event', identity=event.identity, event=event))
        return output

    async def events(self) -> AsyncIterator[GatewayOutput]:
        if self._active:
            raise RunProtocolError('Concurrent Gateway run advancement is not allowed')
        self._active = True
        observations, self._observations = self._observations, []
        terminal = None
        try:
            if self._run.state.status in {'completed', 'failed', 'cancelled'}:
                raise RunProtocolError('A terminal run cannot be resumed')
            if observations:
                await self._diagnostic(DiagnosticRecord(kind='observations.received', identity=self.start.identity,
                                                        observations=[item.model_copy(deep=True) for item in observations]))
            while terminal is None:
                self._check_cancelled()
                finish = False
                if self.orchestration.groups and not self.orchestration.groups[-1].done:
                    group = ForegroundGroup(self, self.orchestration.groups[-1])
                    outgoing = await group.advance(observations)
                    observations = []
                    if outgoing is not None:
                        yield await self._suspend_group(outgoing)
                        self._check_cancelled()
                        return
                    observations = group.parent_observations()
                    finish = self.service.receipt_fast_path and group.fast_completion()
                if not finish and self._run.state.model_calls + self.orchestration.model_calls >= self.start.budget.max_model_calls:
                    raise RunProtocolError('model_call_budget_exhausted')
                async with aclosing(self._run.advance(observations, complete_after_tools=finish)) as stream:
                    async for event in stream:
                        if event.type == 'run.suspended':
                            if not event.data['requests']:
                                raise RunProtocolError('Waiting parent has no orchestration checkpoint')
                            requests = [self._dispatches[item['invocation_id']].model_dump() for item in event.data['requests']]
                            vehicle = (self.orchestration.groups[-1].vehicle_state if self.orchestration.groups
                                       else self.start.context.vehicle.state)
                            self.orchestration.groups.append(GroupCheckpoint(requests=requests, vehicle_state=vehicle))
                            continue
                        await self._diagnostic(DiagnosticRecord(kind='run.event', identity=event.identity,
                                                                event=event.model_copy(deep=True)))
                        if event.type == 'text.delta':
                            yield GatewayOutput(identity=event.identity, sequence=event.sequence,
                                                type=event.type, text=event.data['text'])
                        elif event.type in {'run.completed', 'run.cancelled', 'run.failed'}:
                            terminal = event
                observations = []
        except _DiagnosticsUnavailable:
            # A failed diagnostic handoff must not leave undispatched proposals
            # waiting for receipts. Pending calls remain audit-only in a terminal
            # checkpoint; the failing sink is not called recursively.
            terminal = self._diagnostic_failure()
        except asyncio.CancelledError:
            externally_cancelled = not self._run._cancel.is_set()
            self.cancel()
            self._run.state.status = 'cancelled'
            terminal = self._run._event('run.cancelled')
            try:
                await self._diagnostic(DiagnosticRecord(kind='run.event', identity=self.start.identity, event=terminal))
            except _DiagnosticsUnavailable:
                terminal = self._diagnostic_failure()
            if externally_cancelled:
                raise
        except GeneratorExit:
            # A closed consumer has no next anext() on which to deliver a terminal
            # event. Persist partial output now; a handed-off checkpoint stays valid.
            if self._run.state.status not in {'waiting_tools', 'waiting_reconciliation', 'completed', 'cancelled'}:
                self.cancel()
                self._run.state.status = 'cancelled'
                terminal = self._run._event('run.cancelled', reason='consumer_closed')
                try:
                    await self._diagnostic(DiagnosticRecord(kind='run.event', identity=self.start.identity, event=terminal))
                except _DiagnosticsUnavailable:
                    self._diagnostic_failure()
            else:
                await self._flush_diagnostics()
            raise
        except Exception as error:
            if self._run.state.status in {'completed', 'cancelled', 'failed'}:
                raise
            # Wrong/conflicting continuation receipts are retryable, not a new
            # model decision. Child/model failures terminate this inference.
            if observations and isinstance(error, RunProtocolError):
                raise
            if getattr(error, 'uncertain', False) and self.orchestration.groups:
                commit = self.orchestration.groups[-1].task_commit
                if commit is not None:
                    commit['status'] = 'unknown'
            self._run.state.status, self._run.state.error_code = 'failed', (
                'task_commit_unknown' if getattr(error, 'uncertain', False) else getattr(error, 'code', None) or 'orchestration_error')
            terminal = self._run._event('run.failed', code=self._run.state.error_code, error_type=type(error).__name__)
            try:
                await self._diagnostic(DiagnosticRecord(kind='run.event', identity=self.start.identity, event=terminal))
            except _DiagnosticsUnavailable:
                terminal = self._diagnostic_failure()
        finally:
            self._active = False
        if terminal is not None:
            yield GatewayOutput(identity=terminal.identity, sequence=terminal.sequence,
                                type=terminal.type, error_code=terminal.data.get('code'),
                                checkpoint=self.checkpoint())
