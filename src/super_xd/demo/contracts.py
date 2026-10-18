"""Local cockpit session view."""
from typing import Any
from pydantic import BaseModel, Field
from super_xd.nlp.contracts.catalog import PersonaConfig


class SessionSnapshot(BaseModel):
    session_id: str
    persona: PersonaConfig
    persona_version: int
    vehicle: dict[str, Any]
    tasks: list[dict[str, Any]]
    history: list[dict[str, Any]]
    turns: list[dict[str, Any]] = Field(default_factory=list)
