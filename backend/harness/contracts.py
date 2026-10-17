"""Versioned engine contracts, independent of Gateway transport or storage.

These are application-owned run/checkpoint contracts, not a negotiated wire API.
The caller authenticates and stores them; the harness checks their consistency.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    @model_validator(mode='after')
    def json_value(self):
        # Checkpoint payloads must survive JSON storage without NaN or handles.
        json.dumps(self.model_dump(), allow_nan=False)
        return self


class RunIdentity(Contract):
    run_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    context_revision: int = Field(ge=0)


class RunBudget(Contract):
    """Cumulative model/tool limits; active time counts upstream model waiting.

    Consumer backpressure and external tool waiting are excluded. The host owns
    its end-to-end request deadline independently of this inference budget.
    """
    max_model_calls: int = Field(default=5, ge=1, le=20)
    max_tool_calls: int = Field(default=32, ge=1, le=128)
    active_timeout_seconds: float = Field(default=60.0, gt=0, le=600)


class PreparedRun(Contract):
    """NLP-compiled input. Gateway should supply facts, not system prompts.

    The product layer compiles its approved prompts/tools into this object. This
    low-level engine also supports Planner/Task-specific policies through injection.
    """
    protocol_version: Literal['nlp-internal-v1'] = 'nlp-internal-v1'
    identity: RunIdentity
    messages: list[dict[str, Any]] = Field(min_length=1)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    budget: RunBudget = Field(default_factory=RunBudget)
    release_id: str = Field(min_length=1)
    model_options: dict[str, Any] = Field(default_factory=dict)


class ToolRequest(Contract):
    invocation_id: str
    tool_call_id: str
    name: str
    arguments: dict[str, Any]
    idempotency_key: str


class ToolObservation(Contract):
    identity: RunIdentity
    invocation_id: str
    status: Literal['completed', 'rejected', 'unknown']
    result: dict[str, Any]


class RunCheckpoint(Contract):
    checkpoint_version: Literal[1] = 1
    prepared: PreparedRun
    messages: list[dict[str, Any]]
    status: Literal['ready', 'waiting_tools', 'waiting_reconciliation', 'completed', 'cancelled', 'failed'] = 'ready'
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    active_seconds: float = Field(default=0.0, ge=0)
    sequence: int = Field(default=0, ge=0)
    pending: list[ToolRequest] = Field(default_factory=list)
    observations: dict[str, ToolObservation] = Field(default_factory=dict)
    output: str = ''
    error_code: str | None = None


class RunEvent(Contract):
    identity: RunIdentity
    sequence: int = Field(ge=1)
    type: Literal['text.delta', 'model.started', 'model.completed',
                  'run.suspended', 'run.completed', 'run.cancelled', 'run.failed']
    data: dict[str, Any] = Field(default_factory=dict)
