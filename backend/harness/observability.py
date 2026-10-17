"""Process-local aggregate usage and optional non-generative Gateway readiness."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import asdict, dataclass
import time
from typing import Any
from urllib.parse import ParseResult, urlparse

import httpx

from backend.nlp_settings import NLPSettings
from .model_execution import extract_usage


@dataclass
class ModelUsage:
    calls: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    duration_seconds: float = 0.0
    usage_reports: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    input_usage_reports: int = 0
    output_usage_reports: int = 0
    total_usage_reports: int = 0


class UsageMetrics:
    """Bounded role labels, actual attempts only, and no message/identity storage."""

    def __init__(self) -> None:
        self._roles: defaultdict[str, ModelUsage] = defaultdict(ModelUsage)

    def record(self, role: str, outcome: str, elapsed: float, metadata: dict[str, Any] | None) -> None:
        role = role if role in {'talker', 'planner', 'thinker', 'finalizer'} else 'other'
        item = self._roles[role]
        item.calls += 1
        setattr(item, outcome, getattr(item, outcome) + 1)
        item.duration_seconds += max(0, elapsed)
        usage = extract_usage(metadata)
        values = (('input', usage.input_tokens), ('output', usage.output_tokens), ('total', usage.total_tokens))
        if all(value is None for _, value in values):
            return
        item.usage_reports += 1
        for prefix, value in values:
            if value is not None:
                setattr(item, prefix + '_tokens', getattr(item, prefix + '_tokens') + value)
                setattr(item, prefix + '_usage_reports', getattr(item, prefix + '_usage_reports') + 1)

    def snapshot(self) -> dict[str, Any]:
        return {'scope': 'process', 'models': {role: asdict(value) for role, value in sorted(self._roles.items())}}


class MeasuredLLM:
    """Observe the existing normalized model port without changing its payloads."""

    def __init__(self, model: Any, metrics: UsageMetrics) -> None:
        self.model, self.metrics = model, metrics
        self.config, self.providers = model.config, model.providers

    def request(self, messages: Any, tools: Any = None, thinker: bool = False, **options: Any) -> Any:
        return self.model.request(messages, tools, thinker, **options)

    def for_role(self, role: str) -> MeasuredLLM:
        return MeasuredLLM(self.model.for_role(role), self.metrics)

    def info(self) -> Any:
        return self.model.info()

    async def complete(self, messages: Any, tools: Any = None, thinker: bool = False, **options: Any) -> Any:
        role = self.request(messages, tools, thinker, **options).role
        started, outcome, metadata = time.monotonic(), 'cancelled', None
        try:
            message = await self.model.complete(messages, tools, thinker=thinker, **options)
            metadata, outcome = message.get('_meta'), 'completed'
            return message
        except Exception:
            outcome = 'failed'
            raise
        finally:
            self.metrics.record(role, outcome, time.monotonic() - started, metadata)

    async def stream(self, messages: Any, tools: Any = None, **options: Any) -> AsyncIterator[dict[str, Any]]:
        role = self.request(messages, tools, **options).role
        started, outcome, metadata = time.monotonic(), 'cancelled', None
        try:
            async with aclosing(self.model.stream(messages, tools, **options)) as source:
                async for item in source:
                    if item.get('type') == 'message':
                        metadata, outcome = item.get('meta'), 'completed'
                    yield item
        except Exception:
            outcome = 'failed'
            raise
        finally:
            self.metrics.record(role, outcome, time.monotonic() - started, metadata)


class Readiness:
    """An explicit Gateway health URL is required; no model calls or guessed paths."""

    def __init__(self, settings: NLPSettings, client: httpx.AsyncClient | None = None) -> None:
        self.settings, self.client = settings, client
        self._checked_at: float | None = None
        self._gateway_ok = False
        self._lock = asyncio.Lock()

    async def check(self, *, local_ready: bool) -> dict[str, Any]:
        if not local_ready:
            return {'ready': False, 'local': 'not_ready', 'gateway': 'not_probed'}
        if not self.settings.gateway_health_url:
            return {'ready': True, 'local': 'ready', 'gateway': 'not_probed'}
        async with self._lock:
            now = time.monotonic()
            if self._checked_at is None or now - self._checked_at >= self.settings.readiness_cache_seconds:
                self._gateway_ok = await self._probe()
                self._checked_at = time.monotonic()
        return {'ready': self._gateway_ok, 'local': 'ready',
                'gateway': 'ok' if self._gateway_ok else 'failed'}

    async def _probe(self) -> bool:
        if self.client is None or self.client.is_closed:
            return False
        health, gateway = urlparse(self.settings.gateway_health_url), urlparse(self.settings.gateway_url)
        def origin(parsed: ParseResult) -> tuple[str, str | None, int]:
            return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)
        same_origin = origin(health) == origin(gateway)
        headers = {}
        token = self.settings.gateway_token.get_secret_value()
        if token and same_origin:
            headers['Authorization'] = 'Bearer ' + token
        try:
            async with self.client.stream('GET', self.settings.gateway_health_url, headers=headers,
                    timeout=self.settings.readiness_timeout_seconds, follow_redirects=False) as response:
                return 200 <= response.status_code < 300
        except (httpx.HTTPError, RuntimeError):
            return False
