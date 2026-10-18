from typing import Any, Literal
from pydantic import BaseModel, Field, ConfigDict

class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]
    skill_id: str
    state_mapping: dict[str, Any]
    example: dict[str, Any]
    def openai_tool(self) -> dict[str, object]:
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
