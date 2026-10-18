"""Shared model response framing, cancellation and usage; no Agent lifecycle policy."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, TypeVar, cast

from super_xd.nlp.contracts.providers import ModelRequest, ModelResult as ProviderResult, ModelProvider, ModelEvent

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
class ObservedModelResult:
    message: dict[str, Any]
    metadata: dict[str, Any]
    raw_return: Any
    usage: TokenUsage


def _message_for_run(message: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(message)
    calls = normalized.get('tool_calls')
    if isinstance(calls, list):
        for call in calls:
            function = call.get('function') if isinstance(call, dict) else None
            if isinstance(function, dict) and ('arguments' not in function or function['arguments'] == ''):
                # Providers may omit all argument deltas for a no-argument
                # function. Every consumer receives the same JSON encoding;
                # the untouched provider message remains in raw_return.
                function['arguments'] = '{}'
    return normalized


def normalize_result(returned: Any, *, raw_return: Any = None) -> ObservedModelResult:
    """Validate the single provider result contract without message metadata magic."""
    raw = ({'message': returned.message, 'meta': returned.metadata} if isinstance(returned, ProviderResult) else returned) if raw_return is None else raw_return
    if not isinstance(returned, ProviderResult):
        raise MalformedModelResponse('Model must return ModelResult', raw)
    message, metadata = returned.message, returned.metadata
    if not isinstance(message, dict) or message.get('role') != 'assistant':
        raise MalformedModelResponse('Model must return an assistant message', raw)
    if message.get('content') is not None and not isinstance(message['content'], str):
        raise MalformedModelResponse('Assistant content must be text or null', raw)
    if '_meta' in message:
        raise MalformedModelResponse('Model metadata belongs outside the assistant message', raw)
    if not isinstance(metadata, dict):
        raise MalformedModelResponse('Model metadata must be an object', raw)
    try:
        json.dumps([message, metadata], allow_nan=False)
    except (TypeError, ValueError) as error:
        raise MalformedModelResponse('Model message/metadata must be finite JSON', raw) from error
    return ObservedModelResult(_message_for_run(message), deepcopy(metadata), deepcopy(raw), extract_usage(metadata))


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


async def complete_model(client: ModelProvider, request: ModelRequest,
                         *, cancel: asyncio.Event | None = None) -> ObservedModelResult:
    returned = await await_model(client.complete(deepcopy(request)), cancel)
    return normalize_result(returned)


class ModelStreamReader:
    def __init__(self, source: AsyncIterator[ModelEvent], cancel: asyncio.Event | None) -> None:
        self.source, self.cancel = source, cancel
        self.final: ObservedModelResult | None = None
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
            return cast(dict[str, Any], deepcopy(item))
        if item.get('type') == 'message':
            if self.final is not None:
                raise MalformedModelResponse('Model emitted more than one final message', item)
            self.final = normalize_result(ProviderResult(cast(Any, item.get('message')), cast(Any, item.get('meta', {}))), raw_return=item)
            return {'type': 'message', 'message': deepcopy(self.final.message),
                    'meta': deepcopy(self.final.metadata), 'raw_return': deepcopy(self.final.raw_return)}
        raise MalformedModelResponse('Unknown model stream item type', item)


@asynccontextmanager
async def open_model_stream(client: ModelProvider, request: ModelRequest,
                            *, cancel: asyncio.Event | None = None) -> AsyncIterator[ModelStreamReader]:
    source = client.stream(deepcopy(request))
    try:
        yield ModelStreamReader(source, cancel)
    finally:
        await source.aclose()
