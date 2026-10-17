"""A local Gateway driver for the production NLP service.

The driver commits each checkpoint and its entire handoff before invoking an
executor. Executors own actual device state; they must not echo NLP predictions.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass

from backend.harness.contracts import ToolObservation
from backend.harness.gateway_contracts import DispatchRequest, GatewayCheckpoint, GatewayOutput, ResumeRequest, StartRequest
from backend.harness.run import RunProtocolError


def history_records(messages):
    """Convert provider messages to the factual Gateway history contract."""
    records = []
    for message in messages:
        role = message['role']
        if role == 'assistant':
            calls = [{'id': call['id'], 'name': call['function']['name'],
                      'arguments': json.loads(call['function']['arguments']),
                      **({'extra_content': deepcopy(call['extra_content'])} if 'extra_content' in call else {})}
                     for call in message.get('tool_calls', [])]
            records.append({'role': role, 'content': message.get('content'), 'tool_calls': calls})
        elif role == 'tool':
            records.append({'role': role, 'tool_call_id': message['tool_call_id'],
                            'result': json.loads(message['content'])})
        elif role == 'user':
            records.append(deepcopy(message))
    return records


@dataclass(frozen=True)
class LocalCommit:
    checkpoint: GatewayCheckpoint
    requests: tuple[DispatchRequest, ...]


class LocalGateway:
    """One advancer per run, with stable handoffs and no unknown-action retry."""

    def __init__(self, service, start: StartRequest,
                 execute: Callable[[DispatchRequest, StartRequest], Awaitable[ToolObservation]]):
        self.service, self.start, self.execute = service, start, execute
        self.commit: LocalCommit | None = None
        self.observations: dict[str, ToolObservation] = {}
        self.current_run = None
        self.cancelled = False
        self._active = False

    def cancel(self):
        self.cancelled = True
        if self.current_run is not None:
            self.current_run.cancel()

    async def events(self) -> AsyncIterator[GatewayOutput]:
        if self._active:
            raise RunProtocolError('A local Gateway run already has an advancer')
        self._active = True
        try:
            self.current_run = self.service.start(self.start)
            while not self.cancelled:
                last = None
                async with aclosing(self.current_run.events()) as events:
                    async for event in events:
                        last = event
                        if event.checkpoint is not None:
                            # One assignment is the in-memory transaction boundary.
                            self.commit = LocalCommit(event.checkpoint.model_copy(deep=True),
                                                      tuple(item.model_copy(deep=True) for item in event.requests))
                        yield event
                if last is None or last.type != 'run.suspended' or not last.requests or self.cancelled:
                    return
                if any(item.request.invocation_id in self.observations and
                       self.observations[item.request.invocation_id].status == 'unknown' for item in last.requests):
                    return
                results = await asyncio.gather(*(self._execute_once(item) for item in last.requests))
                if self.cancelled:
                    return
                self.current_run = self.service.resume(ResumeRequest(identity=self.start.identity,
                    checkpoint=self.commit.checkpoint, observations=results))
        finally:
            self._active = False

    async def _execute_once(self, item: DispatchRequest) -> ToolObservation:
        key = item.request.invocation_id
        if key not in self.observations:
            observation = await self.execute(item, self.start)
            if observation.identity != self.start.identity or observation.invocation_id != key:
                raise RunProtocolError('Local executor returned an unrelated observation')
            self.observations[key] = observation.model_copy(deep=True)
        return self.observations[key].model_copy(deep=True)
