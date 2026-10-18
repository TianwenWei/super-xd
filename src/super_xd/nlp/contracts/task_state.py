"""Typed task state, checkpoints and writes shared by all ledger implementations."""
from __future__ import annotations
from typing import Annotated, Any, Literal
from typing_extensions import Required, TypedDict
from pydantic import ConfigDict, Field, TypeAdapter, with_config
from super_xd.nlp.contracts.run import Contract
from super_xd.nlp.contracts.evidence import RetrievalProgress

TaskStatus = Literal['queued', 'running', 'waiting_clarification', 'cancelling',
                     'completed', 'failed', 'cancelled', 'awaiting_confirm']
DeliveryMode = Literal['foreground', 'background']


@with_config(ConfigDict(extra='allow', strict=True))
class TaskContext(TypedDict, total=False):
    release_id: str
    model_policy_id: str
    origin_identity: dict[str, Any] | None
    origin_turn_id: str | None
    speaker: dict[str, Any]
    vehicle: dict[str, Any]
    recent_history: list[dict[str, Any]]
    captured_at: str
    timezone: str
    current_location: dict[str, Any] | None
    query_mode: Literal['quick', 'research']
    query_domain: Literal['weather', 'general']
    spoken_goal: str | None
    read_details: bool
    required_source_domains: list[str]


@with_config(ConfigDict(extra='allow', strict=True))
class TaskResult(TypedDict, total=False):
    summary: str
    answer: str
    spoken_summary: str
    partial: bool
    sources: list[dict[str, Any]]
    suggested_actions: list[dict[str, Any]]
    query_times: list[str]
    web_evidence: Literal['none', 'search_only', 'extracted', 'forecast', 'verified', 'incomplete']
    forecast_records: list[dict[str, Any]]
    tool_observations: list[dict[str, Any]]
    query_mode: Literal['quick', 'research']


class TaskCheckpoint(Contract):
    checkpoint_version: Literal[1] = 1
    release_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    revision: int = Field(ge=1)
    messages: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    suggestions: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    tool_count: int = Field(ge=0)
    retrieval: RetrievalProgress = Field(default_factory=RetrievalProgress)
    constraint_count: int = Field(ge=0)


@with_config(ConfigDict(extra='forbid', strict=True))
class TaskCheckpointData(TypedDict, total=False):
    # Empty means not started. Nonempty checkpoints are validated as TaskCheckpoint.
    checkpoint_version: Literal[1]
    release_id: str
    goal: str
    revision: Annotated[int, Field(ge=1)]
    messages: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    suggestions: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    tool_count: Annotated[int, Field(ge=0)]
    retrieval: dict[str, Any]
    constraint_count: Annotated[int, Field(ge=0)]


@with_config(ConfigDict(extra='forbid', strict=True))
class TaskProgress(TypedDict, total=False):
    phase: str
    read_sources: Annotated[int, Field(ge=0)]


@with_config(ConfigDict(extra='allow', strict=True))
class TaskState(TypedDict, total=False):
    # A ledger may retain extra UI/audit fields, without weakening execution fields.
    id: Required[str]
    session_id: Required[str]
    goal: Required[str]
    revision: Required[Annotated[int, Field(ge=1)]]
    status: Required[TaskStatus]
    delivery_mode: DeliveryMode
    deadline_at: Required[str]
    running_seconds: float
    running_since: str | None
    constraints: list[str]
    context: TaskContext
    steps: Annotated[int, Field(ge=0)]
    checkpoint: TaskCheckpointData
    sources: list[dict[str, Any]]
    transcript: list[dict[str, Any]]
    progress: TaskProgress
    result: TaskResult | None
    summary: str
    question: str | None
    error: str | None
    error_code: str | None


@with_config(ConfigDict(extra='forbid', strict=True))
class TaskPatch(TypedDict, total=False):
    status: TaskStatus
    summary: str
    result: TaskResult | None
    sources: list[dict[str, Any]]
    transcript: list[dict[str, Any]]
    question: str | None
    error: str | None
    error_code: str | None
    steps: Annotated[int, Field(ge=0)]
    checkpoint: TaskCheckpointData
    progress: TaskProgress
    running_since: str | None


TASK_PATCH = TypeAdapter(TaskPatch)
