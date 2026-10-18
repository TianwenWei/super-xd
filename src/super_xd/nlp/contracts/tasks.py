"""Gateway task operations and presentation contracts."""
from typing import Any, Literal
from datetime import datetime
from pydantic import ConfigDict, Field, field_validator
from super_xd.nlp.contracts.run import Contract
from super_xd.nlp.contracts.gateway_v1 import PersonaId
from super_xd.nlp.contracts.task_state import (DeliveryMode, TaskStatus, TaskContext, TaskState, TaskCheckpoint, TaskCheckpointData)


def _aware(value: str | None) -> str | None:
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
    status: TaskStatus
    delivery_mode: DeliveryMode = 'background'
    deadline_at: str
    running_seconds: float = Field(default=0.0, ge=0)
    running_since: str | None = None
    constraints: list[str] = Field(default_factory=list)
    context: TaskContext = Field(default_factory=TaskContext)
    steps: int = Field(default=0, ge=0)
    checkpoint: TaskCheckpointData = Field(default_factory=TaskCheckpointData)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    _timestamp = field_validator('deadline_at', 'running_since')(_aware)

    @field_validator('checkpoint')
    @classmethod
    def valid_checkpoint(cls, value: TaskCheckpointData) -> TaskCheckpointData:
        if value:
            TaskCheckpoint.model_validate(value)
        return value


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
    task: TaskState
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


class TaskDeliveryRequest(Contract):
    """Detach foreground delivery while the same leased executor continues.

    The ledger compares revision and updates delivery mode plus its event in a
    transaction containing the authoritative checkpoint. Execution ownership,
    status and accumulated budgets do not change.
    """
    conversation_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)
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
