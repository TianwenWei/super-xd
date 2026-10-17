"""Frozen production startup settings, read explicitly from the supplied environment."""
from __future__ import annotations

from collections.abc import Mapping
import os
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class NLPSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', allow_inf_nan=False)

    gateway_url: str = ''
    gateway_token: SecretStr = SecretStr('')
    gateway_timeout_seconds: float = Field(default=10, gt=0)
    gateway_health_url: str = ''
    readiness_timeout_seconds: float = Field(default=2, gt=0, le=10)
    readiness_cache_seconds: float = Field(default=5, ge=0, le=60)
    max_body_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    max_inflight: int = Field(default=32, gt=0)
    request_timeout_seconds: float = Field(default=120, gt=0)
    body_timeout_seconds: float = Field(default=30, gt=0)
    thinker_max_steps: int = Field(default=20, gt=0)
    thinker_timeout_seconds: float = Field(default=60, gt=0)
    tool_timeout_seconds: float = Field(default=12, gt=0)

    @field_validator('gateway_url', 'gateway_health_url')
    @classmethod
    def valid_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlparse(value)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or any(char.isspace() for char in value)):
            raise ValueError('Gateway URLs must be HTTP(S) without credentials, query or fragment')
        _ = parsed.port
        return value.rstrip('/')

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> NLPSettings:
        source = os.environ if environment is None else environment
        names = {name: 'NLP_' + name.upper() for name in cls.model_fields}
        names.update(thinker_max_steps='THINKER_MAX_STEPS', thinker_timeout_seconds='THINKER_TIMEOUT_SECONDS',
                     tool_timeout_seconds='TOOL_TIMEOUT_SECONDS')
        return cls.model_validate({name: source[variable] for name, variable in names.items() if variable in source})
