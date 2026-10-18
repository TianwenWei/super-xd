"""Gateway wire models. No runtime, executor, provider or persistence imports."""
from __future__ import annotations
from datetime import datetime
import hashlib
import json
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import Field, field_validator, model_validator
from pydantic_core import PydanticCustomError
from super_xd.nlp.contracts.capabilities import ToolCapability
from super_xd.nlp.contracts.task_state import TaskStatus
from super_xd.nlp.contracts.run import Contract, PreparedRun, RunBudget, RunCheckpoint, RunIdentity, ToolObservation, ToolRequest
from super_xd.nlp.contracts.evidence import RetrievalProgress

GATEWAY_VERSION: Literal['gateway-v1'] = 'gateway-v1'
# One 8 MiB HTTP envelope supports the accepted start and all continuations.
MAX_WIRE_BYTES = 8 * 1024 * 1024
MAX_START_BYTES = 6 * 1024 * 1024
MAX_CONTINUATION_BYTES = 1024 * 1024
MAX_OBSERVATION_BYTES = 256 * 1024


def wire_bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode())

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
    extra_content: dict[str, Any] = Field(default_factory=dict)

    @field_validator('extra_content')
    @classmethod
    def bounded_provider_metadata(cls, value):
        # Gateway stores opaque continuation data, never synthesizes signatures.
        if wire_bytes(value) > 64 * 1024:
            raise ValueError('Historical provider tool metadata exceeds 64 KiB')
        return value


class UserRecord(Contract):
    role: Literal['user']
    content: str = Field(min_length=1, max_length=16000)


class AssistantRecord(Contract):
    role: Literal['assistant']
    content: str | None = None
    tool_calls: list[HistoricalCall] = Field(default_factory=list, max_length=32)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator('provider_metadata')
    @classmethod
    def bounded_continuation(cls, value):
        if set(value) - {'extra_content'}:
            raise ValueError('Unsupported assistant continuation field')
        if 'extra_content' in value and not isinstance(value['extra_content'], dict):
            raise ValueError('Assistant extra_content must be an object')
        if wire_bytes(value) > 64 * 1024:
            raise ValueError('Assistant continuation data exceeds 64 KiB')
        return value


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
    playback_status: Literal['unknown', 'not_started', 'partial', 'completed'] = 'unknown'

    @model_validator(mode='after')
    def valid_protocol(self):
        if self.playback_status == 'not_started' and self.heard_text:
            raise ValueError('Playback not_started cannot include heard_text')
        if self.proactive != (self.source_task is not None):
            raise ValueError('Proactive history requires structured task attribution')
        pending: set[str] = set()
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
        if wire_bytes(self.model_dump()) > MAX_START_BYTES:
            raise PydanticCustomError('start_size_exceeded', 'StartRequest exceeds the 6 MiB input budget')
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
    execution_constraints: dict[str, Any] = Field(default_factory=dict)


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


class VehicleAction(Contract):
    id: str
    intent_id: str
    name: str
    arguments: dict[str, Any]
    depends_on: list[str] = Field(default_factory=list)


class VehicleIntent(Contract):
    id: str
    source_text: str
    source_start: int = Field(ge=0)
    source_end: int = Field(ge=1)
    text: str | None = None
    status: Literal['planned', 'keep', 'clarification', 'refused', 'completed']
    action_ids: list[str]
    completed_operation_ids: list[str] = Field(default_factory=list)
    keep_paths: list[str]
    question: str
    reason: str


class VehiclePlan(Contract):
    status: Literal['planned', 'clarification', 'refused']
    intents: list[VehicleIntent]
    actions: list[VehicleAction]
    keep_paths: list[str]
    question: str
    reason: str
    ok: bool = False
    executed: Literal[False] = False
    loaded_skills: list[str] = Field(default_factory=list)
    model_calls: int = 0
    plan_call_id: str | None = None
    model_call_id: str | None = None
    provenance: dict[str, Any] | None = None
    validation: dict[str, Any] = Field(default_factory=dict)
    action_ledger: list[dict[str, Any]] = Field(default_factory=list)


class PlanReceipt(Contract):
    action_id: str
    name: str
    arguments: dict[str, Any]
    invocation_id: str
    status: Literal['completed', 'rejected', 'unknown']
    result: dict[str, Any]


class CompletedPlan(Contract):
    plan: VehiclePlan
    receipts: list[PlanReceipt]


class TaskCommit(Contract):
    invocation_id: str
    idempotency_key: str
    status: Literal['pending', 'confirmed', 'unknown']
    task_id: str | None = None


class GroupCheckpoint(Contract):
    requests: list[DispatchRequest] = Field(min_length=1)
    cursor: int = Field(default=0, ge=0)
    external_pending: list[DispatchRequest] = Field(default_factory=list)
    receipts: dict[str, ToolObservation] = Field(default_factory=dict)
    results: dict[str, ToolObservation] = Field(default_factory=dict)
    plan: VehiclePlan | None = None
    plan_index: int = Field(default=0, ge=0)
    plan_receipts: list[PlanReceipt] = Field(default_factory=list)
    plan_prior_operations: list[dict[str, Any]] = Field(default_factory=list)
    completed_plans: list[CompletedPlan] = Field(default_factory=list)
    task_commit: TaskCommit | None = None
    vehicle_state: dict[str, dict[str, Any]] = Field(default_factory=dict)
    vehicle_state_delta: bool = False
    keep_baseline: dict[str, Any] = Field(default_factory=dict)
    has_reads: bool = False
    done: bool = False


class OrchestrationCheckpoint(Contract):
    groups: list[GroupCheckpoint] = Field(default_factory=list)
    retrieval: RetrievalProgress = Field(default_factory=RetrievalProgress)
    model_calls: int = Field(default=0, ge=0)
    active_seconds: float = Field(default=0.0, ge=0)
    tool_calls: int = Field(default=0, ge=0)


def prepared_digest(prepared: PreparedRun) -> str:
    payload = json.dumps(prepared.model_dump(), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest()


class CompactRunCheckpoint(Contract):
    """Store only generated messages; rebuild the frozen prefix from StartRequest.

    The digest detects deployment/context mismatch, not malicious Gateway forgery.
    Only authenticated, caller-owned durable storage may return this checkpoint.
    """
    checkpoint_version: Literal[2] = 2
    identity: RunIdentity
    release_id: str
    prepared_digest: str
    messages: list[dict[str, Any]]
    status: Literal['ready', 'waiting_tools', 'waiting_reconciliation', 'completed', 'cancelled', 'failed']
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    active_seconds: float = Field(ge=0)
    sequence: int = Field(ge=0)
    pending: list[ToolRequest]
    observations: dict[str, ToolObservation]
    output: str
    error_code: str | None = None

    @classmethod
    def capture(cls, state: RunCheckpoint):
        payload = state.model_dump(exclude={'prepared', 'checkpoint_version'})
        payload['messages'] = payload['messages'][len(state.prepared.messages):]
        return cls(identity=state.prepared.identity, release_id=state.prepared.release_id,
                   prepared_digest=prepared_digest(state.prepared), **payload)

    def restore(self, prepared: PreparedRun) -> RunCheckpoint:
        if (self.identity != prepared.identity or self.release_id != prepared.release_id
                or self.prepared_digest != prepared_digest(prepared)):
            raise ValueError('Continuation input, release or compiled model context does not match')
        payload = self.model_dump(exclude={'identity', 'release_id', 'prepared_digest', 'checkpoint_version'})
        payload['messages'] = [*prepared.messages, *self.messages]
        return RunCheckpoint(prepared=prepared, **payload)


class CompletionPresentation(Contract):
    speech: str = Field(min_length=1)
    source: Literal['verified_receipts', 'error_template']


class GatewayCheckpoint(Contract):
    protocol_version: Literal['gateway-v1'] = GATEWAY_VERSION
    start: StartRequest
    run: CompactRunCheckpoint
    semantic_policy_id: str = Field(min_length=1, max_length=200)
    model_policy_id: str = Field(min_length=1, max_length=200)
    orchestration: OrchestrationCheckpoint = Field(default_factory=OrchestrationCheckpoint)
    presentation: CompletionPresentation | None = None

    @model_validator(mode='after')
    def same_identity(self):
        if wire_bytes(self.model_dump(exclude={'start'})) > MAX_CONTINUATION_BYTES:
            raise PydanticCustomError('checkpoint_size_exceeded', 'Continuation exceeds the 1 MiB state budget')
        if self.start.identity != self.run.identity:
            raise ValueError('Checkpoint belongs to a different run/context revision')
        if self.presentation is not None and self.run.status not in {'completed', 'failed'}:
            raise ValueError('Only completed or failed runs may have a presentation')
        return self


class ResumeRequest(Contract):
    protocol_version: Literal['gateway-v1'] = GATEWAY_VERSION
    identity: RunIdentity
    checkpoint: GatewayCheckpoint
    observations: list[ToolObservation] = Field(default_factory=list, max_length=128)
    cancel: bool = False

    @model_validator(mode='after')
    def same_run(self):
        if wire_bytes([item.model_dump() for item in self.observations]) > MAX_OBSERVATION_BYTES:
            raise PydanticCustomError('observations_size_exceeded', 'Observation batch exceeds 256 KiB; no observation was accepted')
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
    error_details: dict[str, Any] = Field(default_factory=dict)
    presentation: CompletionPresentation | None = None

    @model_validator(mode='after')
    def coherent_handoff(self):
        if self.type == 'text.delta':
            if self.text is None or self.requests or self.checkpoint is not None or self.presentation is not None:
                raise ValueError('A text delta contains text only')
        elif self.checkpoint is None:
            raise ValueError('Every suspension or terminal event carries a checkpoint')
        if self.checkpoint is not None and self.checkpoint.start.identity != self.identity:
            raise ValueError('Output identity must match its checkpoint')
        if self.requests and self.type != 'run.suspended':
            raise ValueError('Only a suspension may hand off new tool requests')
        if self.checkpoint is not None:
            if self.presentation != self.checkpoint.presentation:
                raise ValueError('Output presentation must match its committed checkpoint')
            state = self.checkpoint.run
            if self.sequence != state.sequence:
                raise ValueError('Output and checkpoint sequence must match')
            statuses = {'run.suspended': {'waiting_tools', 'waiting_reconciliation'},
                        'run.completed': {'completed'}, 'run.failed': {'failed'}, 'run.cancelled': {'cancelled'}}
            if state.status not in statuses[self.type]:
                raise ValueError('Output and checkpoint status must match')
            groups = self.checkpoint.orchestration.groups
            expected = ([item.request for item in groups[-1].external_pending]
                        if groups and not groups[-1].done else state.pending)
            if self.requests and [item.request for item in self.requests] != expected:
                raise ValueError('A handoff must contain its whole pending group')
        return self


class CancelRequest(Contract):
    identity: RunIdentity
