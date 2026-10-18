"""Explicit policy, diagnostics and foreground host boundaries."""
from __future__ import annotations
from collections.abc import Sequence
from typing import Any, Literal, Protocol
from pydantic import Field, model_validator
from super_xd.nlp.contracts.run import Contract, RunCheckpoint, RunEvent, RunIdentity, ToolObservation, ToolRequest
from super_xd.nlp.contracts.gateway_v1 import StartRequest, NLPDispatch, ToolDispatch

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

    def validate_group(self, input_: PolicyInput) -> Sequence[NLPDispatch | ToolDispatch | dict]: ...

    def prepare_product_action(self, input_: dict, request: dict, observations: dict) -> NLPDispatch | ToolDispatch | dict: ...

    def validate_observations(self, input_: dict, observations: dict) -> None: ...


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



class ForegroundHost(Protocol):
    start: StartRequest
    policy: SemanticPolicy

    @property
    def run_state(self) -> RunCheckpoint: ...
    def check_cancelled(self) -> None: ...
    def history(self) -> list[dict]: ...
    def completed_vehicle_operations(self) -> list[dict]: ...
    def reserve_child_tool(self) -> None: ...
    def retrieval_rejection(self, request: ToolRequest, reserved: Sequence[ToolRequest] = ()) -> dict | None: ...
    def accept_retrieval(self, request: ToolRequest, observation: ToolObservation) -> None: ...
    async def plan_vehicle(self, request: ToolRequest, vehicle: dict) -> dict: ...
    async def handle_task(self, request: ToolRequest, arguments: dict | None) -> dict: ...
