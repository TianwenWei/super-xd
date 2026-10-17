"""Shared model response framing, cancellation and usage; no Agent lifecycle policy."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, TypeVar

T = TypeVar('T')


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


def extract_usage(metadata: Any) -> TokenUsage:
    """Unknown usage remains unknown, including providers reporting only totals."""
    usage = metadata.get('usage') if isinstance(metadata, dict) else None
    if not isinstance(usage, dict):
        return TokenUsage()

    def count(primary: str, alternate: str = '') -> int | None:
        value = usage.get(primary, usage.get(alternate))
        return value if type(value) is int and value >= 0 else None

    return TokenUsage(count('input_tokens', 'prompt_tokens'), count('output_tokens', 'completion_tokens'),
                      count('total_tokens'))


def _diagnostic_copy(raw: Any) -> Any:
    try:
        json.dumps(raw, allow_nan=False)
        return deepcopy(raw)
    except (TypeError, ValueError):
        # A malformed Python fixture or non-JSON provider value must not prevent
        # the error itself being serialized into the diagnostic event.
        return {'invalid_json_type': type(raw).__name__, 'representation': repr(raw)}


class MalformedModelResponse(ValueError):
    code = 'invalid_model_message'

    def __init__(self, reason: str, raw: Any) -> None:
        super().__init__(reason)
        self.trace_partial = {'raw_return': _diagnostic_copy(raw)}


@dataclass(frozen=True)
class ModelResult:
    message: dict[str, Any]
    metadata: dict[str, Any]
    raw_return: Any
    usage: TokenUsage


def normalize_result(returned: Any, *, metadata: Any = None, raw_return: Any = None) -> ModelResult:
    """Keep all provider fields for diagnostics; never insert metadata into messages."""
    raw = returned if raw_return is None else raw_return
    if not isinstance(returned, dict) or returned.get('role') != 'assistant':
        raise MalformedModelResponse('Model must return an assistant message', raw)
    if returned.get('content') is not None and not isinstance(returned['content'], str):
        raise MalformedModelResponse('Assistant content must be text or null', raw)
    internal = returned.get('_meta', {})
    if not isinstance(internal, dict) or metadata is not None and not isinstance(metadata, dict):
        raise MalformedModelResponse('Model metadata must be an object', raw)
    merged = deepcopy(internal)
    for key, value in (metadata or {}).items():
        if key in merged and merged[key] != value:
            raise MalformedModelResponse('Conflicting model metadata', raw)
        merged[key] = deepcopy(value)
    message = {key: deepcopy(value) for key, value in returned.items() if key != '_meta'}
    try:
        json.dumps([message, merged], allow_nan=False)
    except (TypeError, ValueError) as error:
        raise MalformedModelResponse('Model message/metadata must be finite JSON', raw) from error
    return ModelResult(message, merged, deepcopy(raw), extract_usage(merged))


async def await_model(operation: Awaitable[T], cancel: asyncio.Event | None = None) -> T:
    """Cancel an in-flight provider operation and retain its observed partial trace."""
    if cancel is None:
        return await operation
    pending = asyncio.ensure_future(operation)
    interrupted = asyncio.create_task(cancel.wait())
    try:
        if not cancel.is_set():
            await asyncio.wait((pending, interrupted), return_when=asyncio.FIRST_COMPLETED)
        if cancel.is_set():
            raise asyncio.CancelledError
        return await pending
    except asyncio.CancelledError as error:
        pending.cancel()
        try:
            late_return = await pending
            setattr(error, 'trace_partial', {'late_return': _diagnostic_copy(late_return)})
        except BaseException as upstream:
            partial = getattr(upstream, 'trace_partial', None)
            if partial is not None:
                setattr(error, 'trace_partial', deepcopy(partial))
            if not isinstance(upstream, asyncio.CancelledError):
                detail = {'error_type': type(upstream).__name__, 'message': str(upstream),
                          'partial': deepcopy(partial)}
                setattr(error, 'trace_partial', {'late_error': detail})
        raise
    finally:
        interrupted.cancel()
        await asyncio.gather(interrupted, return_exceptions=True)


async def complete_model(client: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                         *, cancel: asyncio.Event | None = None, **options: Any) -> ModelResult:
    returned = await await_model(client.complete(deepcopy(messages), tools=deepcopy(tools), **deepcopy(options)), cancel)
    return normalize_result(returned)


class ModelStreamReader:
    def __init__(self, source: AsyncIterator[dict[str, Any]], cancel: asyncio.Event | None) -> None:
        self.source, self.cancel = source, cancel
        self.final: ModelResult | None = None
        self.last_item: Any = None

    def __aiter__(self) -> ModelStreamReader:
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            item = await await_model(anext(self.source), self.cancel)
        except StopAsyncIteration:
            if self.final is None:
                raise MalformedModelResponse('Model stream ended without an assistant message', self.last_item) from None
            raise
        self.last_item = deepcopy(item)
        if not isinstance(item, dict):
            raise MalformedModelResponse('Model stream item must be an object', item)
        if item.get('type') == 'delta':
            if not isinstance(item.get('text'), str) or self.final is not None:
                raise MalformedModelResponse('Invalid or late model text delta', item)
            return deepcopy(item)
        if item.get('type') == 'message':
            if self.final is not None:
                raise MalformedModelResponse('Model emitted more than one final message', item)
            self.final = normalize_result(item.get('message'), metadata=item.get('meta'), raw_return=item)
            return {'type': 'message', 'message': deepcopy(self.final.message),
                    'meta': deepcopy(self.final.metadata), 'raw_return': deepcopy(self.final.raw_return)}
        raise MalformedModelResponse('Unknown model stream item type', item)


@asynccontextmanager
async def open_model_stream(client: Any, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                            *, cancel: asyncio.Event | None = None, **options: Any) -> AsyncIterator[ModelStreamReader]:
    source = client.stream(deepcopy(messages), deepcopy(tools), **deepcopy(options))
    try:
        yield ModelStreamReader(source, cancel)
    finally:
        await source.aclose()
