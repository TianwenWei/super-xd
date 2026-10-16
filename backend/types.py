from typing import Any, Literal
from pydantic import BaseModel, Field, ConfigDict

class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]
    skill_id: str
    state_mapping: dict[str, Any]
    example: dict[str, Any]
    def openai_tool(self):
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters, "strict": True}}

class SkillManifest(BaseModel):
    id: str
    name: str
    description: str
    tool_names: list[str]

class PersonaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default="custom", max_length=40)
    name: str = Field(max_length=40)
    tone: str = Field(max_length=160)
    sentence_length: Literal["short", "medium"] = "short"
    catchphrase_frequency: Literal["never", "low", "normal"] = "low"
    voice: str = Field(default="assistant", max_length=120)

class SessionSnapshot(BaseModel):
    session_id: str
    persona: PersonaConfig
    persona_version: int
    vehicle: dict[str, Any]
    tasks: list[dict[str, Any]]
    history: list[dict[str, Any]]
    turns: list[dict[str, Any]] = Field(default_factory=list)

class TraceEvent(BaseModel):
    trace_id: int
    session_id: str
    turn_id: str | None = None
    task_id: str | None = None
    revision: int | None = None
    call_id: str | None = None
    step: int | None = None
    kind: str
    status: str | None = None
    created_at: str
    data: dict[str, Any]

class TaskEvent(BaseModel):
    event_id: int
    task_id: str
    session_id: str
    revision: int
    kind: str
    payload: dict[str, Any]
    created_at: str
