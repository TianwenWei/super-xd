"""Bounded, resumable model/tool loop without sessions, devices or a database.

Tool requests are proposals. Only injected observations close their protocol
groups; this module never executes tools, declares physical success or retries
an operation whose outcome is unknown. Product-specific semantic validation is
injected before a whole tool group is exposed to an executor.
"""
from __future__ import annotations

import asyncio
from contextlib import aclosing
from copy import deepcopy
import hashlib
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Protocol

from jsonschema import Draft202012Validator

from .contracts import PreparedRun, RunCheckpoint, RunEvent, RunIdentity, ToolObservation, ToolRequest


class ModelStream(Protocol):
    def stream(self, messages: list[dict], tools: list[dict], **options) -> AsyncIterator[dict]: ...


class RunProtocolError(ValueError):
    pass


class ModelBudgetExceeded(TimeoutError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


async def _next_item(iterator, cancel):
    if cancel.is_set():
        raise asyncio.CancelledError
    pending = asyncio.create_task(anext(iterator))
    interrupted = asyncio.create_task(cancel.wait())
    try:
        await asyncio.wait((pending, interrupted), return_when=asyncio.FIRST_COMPLETED)
        if cancel.is_set():
            raise asyncio.CancelledError
        return await pending
    finally:
        pending.cancel()
        interrupted.cancel()
        await asyncio.gather(pending, interrupted, return_exceptions=True)


class AgentRun:
    """One active owner per checkpoint; storage/dispatch ownership is external.

    Call advance until it suspends, persist the checkpoint, then construct a new
    instance to resume with matching observations. Missing/partial observations do
    not re-emit requests. A suspension contains the entire new request group and
    checkpoint in one event. The dispatcher stores requests with the checkpoint
    and uses their stable idempotency keys when delivering them.
    """

    def __init__(self, prepared: PreparedRun, model: ModelStream, *, checkpoint=None,
                 validate_group: Callable[[list[ToolRequest]], None] | None = None,
                 prepare_messages: Callable[[list[dict]], list[dict]] | None = None):
        self.prepared = PreparedRun.model_validate(prepared.model_dump())
        self.model = model
        self.validate_group = validate_group
        self.prepare_messages = prepare_messages
        self._active = False
        self._cancel = asyncio.Event()
        if checkpoint is None:
            self.state = RunCheckpoint(prepared=self.prepared, messages=deepcopy(prepared.messages))
        else:
            self.state = RunCheckpoint.model_validate(
                checkpoint.model_dump() if isinstance(checkpoint, RunCheckpoint) else checkpoint)
            if self.state.prepared != self.prepared:
                raise RunProtocolError('Continuation input, release or context revision does not match')
        self.validators = {}
        for tool in prepared.tools:
            function = tool.get('function', {})
            name, schema = function.get('name'), function.get('parameters')
            if tool.get('type') != 'function' or not isinstance(name, str) or not name or name in self.validators or not isinstance(schema, dict):
                raise RunProtocolError('Tools require unique function names and parameter schemas')
            Draft202012Validator.check_schema(schema)
            self.validators[name] = Draft202012Validator(schema)
        self._validate_checkpoint()

    def _invocation_id(self, tool_call_id):
        identity = canonical([self.prepared.identity.model_dump(), self.state.model_calls, tool_call_id])
        return hashlib.sha256(identity.encode()).hexdigest()

    def _validate_checkpoint(self):
        state = self.state
        if state.model_calls > self.prepared.budget.max_model_calls or state.tool_calls > self.prepared.budget.max_tool_calls:
            raise RunProtocolError('Checkpoint exceeds the original budget')
        if state.messages[:len(self.prepared.messages)] != self.prepared.messages:
            raise RunProtocolError('Checkpoint changed the original model context')
        pending = {item.invocation_id: item for item in state.pending}
        if len(pending) != len(state.pending) or not set(state.observations) <= set(pending):
            raise RunProtocolError('Invalid pending tool group in checkpoint')
        if state.status in {'waiting_tools', 'waiting_reconciliation'} and not pending:
            raise RunProtocolError('Waiting checkpoint has no pending tools')
        if pending and state.status not in {'waiting_tools', 'waiting_reconciliation', 'cancelled', 'failed'}:
            raise RunProtocolError('Pending tools require a waiting or stopped checkpoint')
        if pending:
            if state.model_calls < 1 or state.tool_calls < len(pending):
                raise RunProtocolError('Pending tool group has no accounted model/tool calls')
            message = state.messages[-1]
            calls = message.get('tool_calls', [])
            if message.get('role') != 'assistant' or len(calls) != len(pending):
                raise RunProtocolError('Pending tool group does not match the model transcript')
            for call, request in zip(calls, state.pending):
                fn = call.get('function', {})
                if (call.get('id') != request.tool_call_id or fn.get('name') != request.name
                        or json.loads(fn.get('arguments') or '{}') != request.arguments
                        or request.invocation_id != self._invocation_id(request.tool_call_id)
                        or request.idempotency_key != request.invocation_id or request.name not in self.validators):
                    raise RunProtocolError('Pending request differs from its original tool call')
                self.validators[request.name].validate(request.arguments)
        for identifier, observation in state.observations.items():
            if observation.identity != self.prepared.identity or observation.invocation_id != identifier:
                raise RunProtocolError('Stored tool observation does not belong to this run/revision')

    def checkpoint(self):
        if self._active:
            raise RunProtocolError('Persist the checkpoint carried by a suspension event')
        return self.state.model_copy(deep=True)

    def cancel(self):
        self._cancel.set()

    def _event(self, kind, **data):
        self.state.sequence += 1
        return RunEvent(identity=self.prepared.identity, sequence=self.state.sequence, type=kind, data=data)

    def _suspended(self, new_requests=()):
        event = self._event('run.suspended', status=self.state.status,
                            requests=[request.model_dump() for request in new_requests])
        event.data['checkpoint'] = self.state.model_dump()
        return event

    def _observations(self, observations):
        accepted = deepcopy(self.state.observations)
        pending = {request.invocation_id: request for request in self.state.pending}
        for supplied in observations:
            result = ToolObservation.model_validate(supplied.model_dump() if isinstance(supplied, ToolObservation) else supplied)
            if result.identity != self.prepared.identity or result.invocation_id not in pending:
                raise RunProtocolError('Tool observation does not belong to this run/revision/group')
            previous = accepted.get(result.invocation_id)
            if previous is not None and previous != result and previous.status != 'unknown':
                raise RunProtocolError('Conflicting result for an already resolved tool invocation')
            accepted[result.invocation_id] = result
        self.state.observations = accepted
        if not pending:
            return
        if any(item.status == 'unknown' for item in accepted.values()):
            self.state.status = 'waiting_reconciliation'
            return
        if set(accepted) != set(pending):
            self.state.status = 'waiting_tools'
            return
        for request in self.state.pending:
            observation = accepted[request.invocation_id]
            body = deepcopy(observation.result)
            if observation.status == 'rejected':
                body['ok'] = False
            self.state.messages.append({'role': 'tool', 'tool_call_id': request.tool_call_id,
                                        'content': canonical(body)})
        self.state.pending = []
        self.state.observations = {}
        self.state.status = 'ready'

    def _requests(self, message):
        calls = message.get('tool_calls', [])
        if not isinstance(calls, list):
            raise RunProtocolError('Model tool_calls must be an array')
        requests, identifiers = [], set()
        for call in calls:
            if not isinstance(call, dict) or call.get('type') != 'function':
                raise RunProtocolError('Invalid model tool call')
            identifier, fn = call.get('id'), call.get('function')
            if not isinstance(identifier, str) or not identifier or identifier in identifiers or not isinstance(fn, dict):
                raise RunProtocolError('Tool calls require unique IDs within a group')
            identifiers.add(identifier)
            name = fn.get('name')
            if name not in self.validators:
                raise RunProtocolError('Model requested an unavailable tool')
            arguments = json.loads(fn.get('arguments') or '{}')
            if not isinstance(arguments, dict):
                raise RunProtocolError('Tool arguments must be an object')
            canonical(arguments)
            self.validators[name].validate(arguments)
            invocation = self._invocation_id(identifier)
            requests.append(ToolRequest(invocation_id=invocation, tool_call_id=identifier,
                                        name=name, arguments=arguments, idempotency_key=invocation))
        if self.state.tool_calls + len(requests) > self.prepared.budget.max_tool_calls:
            raise RunProtocolError('tool_call_budget_exhausted')
        if self.validate_group:
            self.validate_group(deepcopy(requests))
        return requests

    async def _next_model_item(self, stream):
        # Never hold a timeout across an outward yield: the caller may be saving
        # an event or waiting for transport capacity in the same asyncio task.
        remaining = self.prepared.budget.active_timeout_seconds - self.state.active_seconds
        if remaining <= 0:
            raise ModelBudgetExceeded
        started = time.monotonic()
        deadline = asyncio.timeout(remaining)
        try:
            async with deadline:
                return await _next_item(stream, self._cancel)
        except TimeoutError as error:
            if deadline.expired():
                raise ModelBudgetExceeded from error
            raise
        finally:
            self.state.active_seconds += time.monotonic() - started

    async def advance(self, observations=(), *, complete_after_tools=False):
        if self._active:
            raise RunProtocolError('Concurrent advance on a single run is not allowed')
        self._active = True
        model_call_active = False
        try:
            if self.state.status in {'completed', 'failed', 'cancelled'}:
                raise RunProtocolError('A terminal run cannot be resumed')
            if self._cancel.is_set():
                self.state.status = 'cancelled'
                yield self._event('run.cancelled')
                return
            had_pending = bool(self.state.pending)
            self._observations(observations)
            if self.state.pending:
                yield self._suspended()
                if self._cancel.is_set():
                    self.state.status = 'cancelled'
                    yield self._event('run.cancelled')
                return
            if complete_after_tools:
                if not had_pending:
                    raise RunProtocolError('Receipt completion requires a pending tool group')
                self.state.status = 'completed'
                yield self._event('run.completed', receipt_fast_path=True, model_calls=self.state.model_calls,
                                  tool_calls=self.state.tool_calls, active_seconds=self.state.active_seconds)
                return
            if self.state.model_calls >= self.prepared.budget.max_model_calls:
                self.state.status, self.state.error_code = 'failed', 'model_call_budget_exhausted'
                yield self._event('run.failed', code=self.state.error_code)
                return
            remaining = self.prepared.budget.active_timeout_seconds - self.state.active_seconds
            if remaining <= 0:
                self.state.status, self.state.error_code = 'failed', 'active_time_budget_exhausted'
                yield self._event('run.failed', code=self.state.error_code)
                return
            model_call_active = True
            request_messages = deepcopy(self.state.messages)
            if self.prepare_messages is not None:
                request_messages = self.prepare_messages(request_messages)
            self.state.model_calls += 1
            yield self._event('model.started', model_call=self.state.model_calls,
                              release_id=self.prepared.release_id, messages=deepcopy(request_messages),
                              tools=deepcopy(self.prepared.tools))
            if self._cancel.is_set():
                raise asyncio.CancelledError
            message = None
            async with aclosing(self.model.stream(deepcopy(request_messages), deepcopy(self.prepared.tools),
                                                  **deepcopy(self.prepared.model_options))) as stream:
                while True:
                    try:
                        item = await self._next_model_item(stream)
                    except StopAsyncIteration:
                        break
                    if item.get('type') == 'delta':
                        value = item.get('text')
                        if not isinstance(value, str):
                            raise RunProtocolError('Model text delta must be a string')
                        self.state.output += value
                        yield self._event('text.delta', text=value)
                    elif item.get('type') == 'message':
                        if message is not None:
                            raise RunProtocolError('Model emitted more than one final message')
                        message = deepcopy(item['message'])
                        yield self._event('model.completed', message=message, metadata=deepcopy(item.get('meta', {})))
            if self._cancel.is_set():
                raise asyncio.CancelledError
            if not isinstance(message, dict) or message.get('role') != 'assistant':
                raise RunProtocolError('Model stream ended without an assistant message')
            requests = self._requests(message)
            self.state.messages.append(message)
            if requests:
                self.state.pending = requests
                self.state.tool_calls += len(requests)
                self.state.status = 'waiting_tools'
                if self._cancel.is_set():
                    raise asyncio.CancelledError
                yield self._suspended(requests)
                if self._cancel.is_set():
                    self.state.status = 'cancelled'
                    yield self._event('run.cancelled')
            else:
                self.state.status = 'completed'
                yield self._event('run.completed', message=message, model_calls=self.state.model_calls,
                                  tool_calls=self.state.tool_calls, active_seconds=self.state.active_seconds)
        except asyncio.CancelledError:
            self.state.status = 'cancelled'
            if self._cancel.is_set():
                yield self._event('run.cancelled')
            else:
                raise
        except GeneratorExit:
            if self.state.status == 'ready':
                self.state.status, self.state.error_code = 'failed', 'stream_abandoned'
            raise
        except Exception as error:
            # Invalid continuation input must remain retryable with correct input.
            if not model_call_active:
                raise
            self.state.status = 'failed'
            self.state.error_code = ('active_time_budget_exhausted' if isinstance(error, ModelBudgetExceeded)
                                     else 'model_timeout' if isinstance(error, TimeoutError)
                                     else 'model_or_protocol_error')
            yield self._event('run.failed', code=self.state.error_code, error_type=type(error).__name__)
        finally:
            self._active = False
