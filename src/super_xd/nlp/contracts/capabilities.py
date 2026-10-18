"""Capability facts shared by deployment and Gateway contracts."""
from typing import Literal
from pydantic import Field
from super_xd.nlp.contracts.run import Contract


class ToolCapability(Contract):
    name: str = Field(min_length=1, max_length=128)
    operation: Literal['query', 'control']
    execution_location: Literal['cloud', 'device']
    domain: str = Field(min_length=1, max_length=64)
    action: str | None = None
    content_kind: str | None = None
    available: bool = True
