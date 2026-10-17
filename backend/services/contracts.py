"""Provider-neutral boundaries; adapters own transport and provider-specific data."""
from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolCapability:
    name: str
    operation: str
    execution_location: str
    domain: str
    action: str | None = None
    content_kind: str | None = None

    def __post_init__(self):
        if self.operation not in {'query', 'control'} or self.execution_location not in {'cloud', 'device'}:
            raise ValueError('Tool capabilities require explicit operation and execution location')

    def to_dict(self):
        return asdict(self)


def redact(value: Any, secrets=()):
    """Keep raw diagnostics useful without carrying credentials across boundaries."""
    sensitive = {'authorization', 'api_key', 'apikey', 'access_token', 'token', 'secret', 'password'}
    if isinstance(value, Mapping):
        return {key: '[redacted]' if str(key).lower() in sensitive else redact(item, secrets)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if isinstance(secret, str) and secret:
                value = value.replace(secret, '[redacted]')
    return value


class ProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, provider: str,
                 retryable: bool = False, details: dict | None = None):
        self.code, self.provider, self.retryable = code, provider, retryable
        self.details = details or {}
        super().__init__(message)

    def public(self):
        return {'code': self.code, 'provider': self.provider, 'message': str(self),
                'retryable': self.retryable}


@dataclass(frozen=True)
class ProviderCapabilities:
    streaming: bool = False
    tools: bool = False
    tool_choice: bool = False
    partial_results: bool = False
    alignment: bool = False


@dataclass(frozen=True)
class ProviderStatus:
    provider: str
    configured: bool
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    details: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class LLMRequest:
    messages: list[dict]
    tools: list[dict] | None = None
    role: str = 'talker'
    model: str | None = None
    reasoning: bool | None = None
    max_tokens: int | None = None
    tool_choice: Any = None
    first_output_timeout_seconds: float | None = None
    total_timeout_seconds: float | None = None


@dataclass(frozen=True)
class LLMResult:
    message: dict
    metadata: dict = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResult: ...
    def stream(self, request: LLMRequest) -> AsyncIterator[dict]: ...
    def info(self) -> dict: ...
    async def aclose(self) -> None: ...


@runtime_checkable
class WebSearchProvider(Protocol):
    async def search(self, query: str, max_results: int = 5, *, include_domains=None) -> dict: ...
    def info(self) -> dict: ...
    async def aclose(self) -> None: ...


@runtime_checkable
class WebContentProvider(Protocol):
    async def extract(self, urls: list[str]) -> dict: ...
    async def aclose(self) -> None: ...


@runtime_checkable
class PlacesProvider(Protocol):
    async def search_places(self, query: str, max_results: int = 5, *, cities_only=False, near=None) -> dict: ...
    def info(self) -> dict: ...
    async def aclose(self) -> None: ...


@runtime_checkable
class WeatherProvider(Protocol):
    async def get_weather(self, location: str, date: str) -> dict: ...
    async def forecast(self, city: str, date: str, country_code: str | None = None) -> dict: ...
    def info(self) -> dict: ...
    async def aclose(self) -> None: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    model: str
    dimensions: int
    async def embed(self, text: str, *, query=False) -> dict: ...
    async def aclose(self) -> None: ...


@runtime_checkable
class RoutesProvider(Protocol):
    async def route(self, origin: dict | None, destination: dict, waypoints: list[dict]) -> dict: ...
    def info(self) -> dict: ...
    async def aclose(self) -> None: ...


@runtime_checkable
class MediaCatalog(Protocol):
    async def search(self, kind: str, query: str, max_results: int = 5) -> dict: ...
    async def resolve(self, content_id: str) -> dict: ...
    def info(self) -> dict: ...
    async def aclose(self) -> None: ...


@runtime_checkable
class ManualProvider(Protocol):
    async def search_manual(self, query: str, max_results: int = 5) -> dict: ...
    async def prepare(self) -> dict: ...
    async def with_page_images(self, result: dict) -> dict: ...
    async def document(self): ...
    async def page_image(self, manual_id: str, page: int): ...
    def info(self) -> dict: ...
    async def aclose(self) -> None: ...
