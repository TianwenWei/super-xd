"""Versioned facts used to decide when a task notification may be presented."""
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, field_serializer

from super_xd.nlp.contracts.run import Contract


class TaskNotice(Contract):
    event_id: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    status: str = Field(min_length=1, max_length=40)
    kind: Literal['progress', 'clarification', 'result', 'failed', 'cancelled']
    priority: int = Field(ge=1, le=3)
    created_at: datetime
    expires_at: datetime

    @field_validator('created_at', 'expires_at')
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('Notice timestamps require an explicit timezone')
        return value

    @field_serializer('created_at', 'expires_at')
    def timestamp(self, value: datetime) -> str:
        return value.isoformat()


class SpeakingContext(Contract):
    connected: bool = True
    user_speaking: bool = False
    assistant_speaking: bool = False
    generating: bool = False
    in_call: bool = False
    demanding_driving: bool = False


class PresentationDecision(Contract):
    action: Literal['speak', 'defer', 'screen_only', 'drop']
    reason: str
