"""NLP service composition and receipt-driven run advancement."""
from __future__ import annotations
from collections.abc import AsyncIterator
from contextlib import aclosing
import asyncio
from copy import deepcopy
from datetime import datetime
import json
import time
from zoneinfo import ZoneInfo
import anyio
from pydantic import ValidationError
from backend.persona import PERSONAS, style_prompt
from backend.catalog import TOOLS
from backend.model_context import compact_message
from backend.speaker import speaker_context
from .context import ContextTurn
from .contracts import PreparedRun, RunCheckpoint, ToolRequest, ToolGroupRejected
from .gateway_contracts import (StartRequest, ResumeRequest, GatewayCheckpoint, GatewayOutput,
    CompactRunCheckpoint, DispatchRequest, ToolDispatch, NLPDispatch, GroupCheckpoint, OrchestrationCheckpoint,
    MAX_CONTINUATION_BYTES, wire_bytes)
from .ports import PolicyInput, SemanticPolicy, DiagnosticRecord, DiagnosticSink
from .run import AgentRun, ModelStream, RunProtocolError
from .release import get_release_id
from .diagnostics import safe_diagnostic
from .talker import prepare_talker_run
from .foreground import ForegroundGroup
from .presentation import completion_presentation
from .product_policy import ProductPolicyError
from .evidence import RetrievalLimits, RetrievalProgress, RETRIEVAL_TOOLS, allow_retrieval, record_retrieval, retrieval_feedback, annotate_evidence

class _DiagnosticsUnavailable(RuntimeError):
    pass


class NLPService:
    def __init__(self, model: ModelStream, *, model_policy_id: str, semantic_policy: SemanticPolicy,
                 release_id: str | None = None, diagnostics: DiagnosticSink | None = None,
                 planner=None, task_service=None, receipt_fast_path: bool = True):
        release_id = get_release_id() if release_id is None else release_id
        identities = (release_id, model_policy_id, getattr(semantic_policy, 'policy_id', None))
        if any(not isinstance(value, str) or not value.strip() for value in identities) or any(not callable(getattr(semantic_policy, method, None)) for method in
            ('validate_group', 'prepare_product_action', 'validate_observations')):
            raise ValueError('Application/model releases and an explicit semantic policy are required')
        if len(model_policy_id) > 200 or len(semantic_policy.policy_id) > 200:
            raise ValueError('Deployment policy identities must be at most 200 characters')
        self.model, self.release_id, self.semantic_policy = model, release_id, semantic_policy
        self.model_policy_id = model_policy_id
        self.diagnostics = diagnostics
        self.planner, self.task_service = planner, task_service
        self.receipt_fast_path = receipt_fast_path

    async def execute_task(self, request):
        if self.task_service is None:
            raise RunProtocolError('Task service is not configured')
        return await self.task_service.execute(request)

    async def mutate_task(self, request):
        if self.task_service is None:
            raise RunProtocolError('Task service is not configured')
        return await self.task_service.mutate(request)

    async def present_task(self, request):
        if self.task_service is None:
            raise RunProtocolError('Task service is not configured')
        return await self.task_service.present(request)

    def compile_start(self, request: StartRequest) -> PreparedRun:
        request = StartRequest.model_validate(request.model_dump())
        context = request.context
        environment = context.environment
        vehicle = context.vehicle.model_dump()['state']
        meta = vehicle.setdefault('_meta', {})
        meta.update(timezone=environment.timezone,
                    location=environment.location.name if environment.location else '',
                    location_country=environment.location.country if environment.location else '')
        local = datetime.fromisoformat(request.received_at).astimezone(ZoneInfo(environment.timezone))
        snapshot = {'vehicle': vehicle,
                    'vehicle_state_revision': context.vehicle.revision,
                    'vehicle_observed_at': context.vehicle.observed_at,
                    'context_revision': context.revision, 'context_observed_at': context.observed_at,
                    'capability_revision': context.capabilities.revision,
                    'capabilities_observed_at': context.capabilities.observed_at,
                    'background_tasks_reference_only': [item.model_dump() for item in context.tasks],
                    'speaker': speaker_context(request.speaker_seat),
                    'recent_places': [item.model_dump() for item in context.places],
                    'media_choices': [item.model_dump() for item in context.media],
                    'cockpit': context.cockpit,
                    'cockpit_capabilities': context.capabilities.cockpit,
                    'tool_capabilities': [item.model_dump() for item in context.capabilities.tools],
                    'capabilities': context.capabilities.features,
                    'current_location': environment.location.model_dump() if environment.location else None,
                    'current_utc': request.received_at, 'current_local': local.isoformat(timespec='seconds'),
                    'current_date': local.date().isoformat(), 'timezone': environment.timezone}
        current = ContextTurn(request.identity.turn_id, [{'role': 'user', 'content': request.user_text}],
                              speaker_seat=request.speaker_seat)
        return prepare_talker_run(request.identity, current_turn=current,
                                  turns=[*(turn.context_turn() for turn in request.history), current],
                                  snapshot=snapshot, persona_prompt=style_prompt(PERSONAS[request.persona_id]),
                                  release_id=self.release_id, budget=request.budget)

    def transcript(self, checkpoint: GatewayCheckpoint) -> list[dict]:
        """Original model transcript, including the compiled prefix, for trusted audit."""
        if checkpoint.semantic_policy_id != self.semantic_policy.policy_id:
            raise RunProtocolError('Transcript requires the original semantic policy release')
        if checkpoint.model_policy_id != self.model_policy_id:
            raise RunProtocolError('Transcript requires the original deployed model policy')
        prepared = self.compile_start(checkpoint.start)
        try:
            return deepcopy(checkpoint.run.restore(prepared).messages)
        except ValueError as error:
            raise RunProtocolError(str(error)) from error

    def turn_records(self, checkpoint: GatewayCheckpoint) -> list[dict]:
        """Current user input and generated records, without examples/history/snapshot."""
        self.transcript(checkpoint)  # Reject a mismatched compiled context before adoption.
        records = [{'role': 'user', 'content': checkpoint.start.user_text}, *deepcopy(checkpoint.run.messages)]
        pending = set()
        for record in checkpoint.run.messages:
            if record.get('role') == 'assistant':
                pending.update(call['id'] for call in record.get('tool_calls', []))
            elif record.get('role') == 'tool':
                pending.discard(record['tool_call_id'])
        # A failed run may retain unclosed calls for audit. Do not manufacture
        # their receipts just to append speech; presentation/playback stay separate.
        if checkpoint.presentation is not None and not pending:
            records.append({'role': 'assistant', 'content': checkpoint.presentation.speech})
        return records

    def start(self, request: StartRequest) -> GatewayRun:
        return GatewayRun(self, request, self.compile_start(request))

    def resume(self, request: ResumeRequest) -> GatewayRun:
        request = ResumeRequest.model_validate(request.model_dump())
        checkpoint = request.checkpoint
        # Receipts occur in the ledger, child result and transcript. Reserve their
        # worst-case expansion before accepting any external fact or making a call.
        added = wire_bytes([item.model_dump() for item in request.observations])
        if wire_bytes(checkpoint.model_dump(exclude={'start'})) + 6 * added > MAX_CONTINUATION_BYTES:
            raise RunProtocolError('checkpoint_size_exceeded: receipt batch does not fit remaining continuation budget; no observation accepted',
                                   code='checkpoint_size_exceeded')
        if checkpoint.run.status in {'completed', 'failed', 'cancelled'}:
            raise RunProtocolError('A terminal run cannot be resumed')
        if checkpoint.semantic_policy_id != self.semantic_policy.policy_id:
            raise RunProtocolError('Continuation requires the original semantic policy release')
        if checkpoint.model_policy_id != self.model_policy_id:
            raise RunProtocolError('Continuation requires the original deployed model policy')
        prepared = self.compile_start(checkpoint.start)
        try:
            restored = checkpoint.run.restore(prepared)
        except ValueError as error:
            raise RunProtocolError(str(error)) from error
        return GatewayRun(self, checkpoint.start, prepared, checkpoint=restored,
                          observations=request.observations, cancel=request.cancel,
                          orchestration=checkpoint.orchestration)


class GatewayRun:
    """NLP-owned child orchestration with caller-owned durable checkpoints."""

    def _model_messages(self, messages):
        # The raw transcript remains in the checkpoint for product receipt
        # replay and audit. Only the actual model request drops task internals.
        prefix = len(self._run.prepared.messages)
        context = {}
        for record in messages[prefix:]:
            for call in record.get('tool_calls', []):
                function = call.get('function', {})
                arguments = json.loads(function.get('arguments', '{}'))
                context[call['id']] = (function.get('name'), arguments.get('read_details') is True)
        result = messages[:prefix]
        for record in messages[prefix:]:
            name, details = context.get(record.get('tool_call_id'), (None, False))
            compact = compact_message(record, name, details)
            if record.get('role') == 'tool':
                original = json.loads(record['content'])
                if isinstance(original, dict) and name in RETRIEVAL_TOOLS:
                    body = annotate_evidence(json.loads(compact['content']), name)
                    body['retrieval_progress'] = retrieval_feedback(self.orchestration.retrieval, name)['retrieval_progress']
                    compact['content'] = json.dumps(body, ensure_ascii=False, separators=(',', ':'))
                if isinstance(original, dict) and 'state' in original:
                    # Unlike the demo actor, a resumed run does not rebuild its
                    # original snapshot. Its current query facts stay visible.
                    body = json.loads(compact['content'])
                    body['state'] = original['state']
                    compact['content'] = json.dumps(body, ensure_ascii=False, separators=(',', ':'))
            result.append(compact)
        return result

    def __init__(self, service, start, prepared, *, checkpoint=None, observations=(), cancel=False,
                 orchestration=None):
        self.service = service
        self.start = StartRequest.model_validate(start.model_dump())
        self.policy = service.semantic_policy
        self.model_policy_id = service.model_policy_id
        self.diagnostics = service.diagnostics
        self._diagnostic_pending = []
        self._diagnostic_bytes = 0
        self._diagnostic_failed = False
        self._presentation = None
        self._dispatches = {}
        self._observations = list(observations)
        self._run = AgentRun(prepared, service.model, checkpoint=checkpoint, validate_group=self._validate_group,
                             prepare_messages=self._model_messages)
        self.orchestration = OrchestrationCheckpoint.model_validate(
            orchestration.model_dump() if orchestration is not None else {})
        for group in self.orchestration.groups:
            if group.vehicle_state_delta:
                group.vehicle_state = {**deepcopy(self.start.context.vehicle.state), **group.vehicle_state}
                group.vehicle_state_delta = False
        self._child = None
        self._active = False
        self._validate_orchestration()
        if cancel:
            self._run.cancel()

    def _validate_orchestration(self):
        state, budget = self._run.state, self._run.prepared.budget
        if state.model_calls + self.orchestration.model_calls > budget.max_model_calls:
            raise RunProtocolError('Child model calls exceed the original budget')
        if state.tool_calls + self.orchestration.tool_calls > budget.max_tool_calls:
            raise RunProtocolError('Child tool calls exceed the original budget')
        for group in self.orchestration.groups:
            try:
                ForegroundGroup(self, group)
            except ValueError as error:
                raise RunProtocolError(str(error)) from error
            for item in group.requests:
                DispatchRequest.model_validate(item)
            for item in group.external_pending:
                DispatchRequest.model_validate(item)
            if any(item.identity != self.start.identity for item in [*group.receipts.values(), *group.results.values()]):
                raise RunProtocolError('Child receipts belong to another run')
        expected_progress = RetrievalProgress()
        for group in self.orchestration.groups:
            for item in group.requests:
                receipt = group.receipts.get(item.request.invocation_id)
                if (item.request.name in RETRIEVAL_TOOLS and item.request.invocation_id in group.results
                        and receipt is not None and receipt.status != 'unknown'):
                    expected_progress, _ = record_retrieval(expected_progress, invocation_id=receipt.invocation_id,
                        tool_name=item.request.name, result=receipt.result)
        if expected_progress != self.orchestration.retrieval:
            raise RunProtocolError('Retrieval progress differs from the accepted receipt ledger')
        if self.orchestration.groups and not self.orchestration.groups[-1].done:
            group = self.orchestration.groups[-1]
            if [item.request for item in group.requests] != state.pending:
                raise RunProtocolError('Orchestration group differs from the original model calls')

    def _validate_group(self, requests):
        input_ = PolicyInput(start=self.start.model_copy(deep=True),
                             checkpoint=self._run.state.model_copy(deep=True),
                             requests=[item.model_copy(deep=True) for item in requests])
        try:
            targets = list(self.policy.validate_group(input_))
        except ProductPolicyError as error:
            if error.result.get('retry_model') is True:
                raise ToolGroupRejected(error.result) from error
            raise
        if len(targets) != len(requests):
            raise RunProtocolError('Semantic policy must decide the entire tool group')
        checked = [DispatchRequest(request=request, dispatch=target) for request, target in zip(requests, targets)]
        self._dispatches = {item.request.invocation_id: item for item in checked}

    def cancel(self):
        self._run.cancel()
        if self._child is not None:
            self._child.cancel()

    def _checkpoint(self, state):
        orchestration = self.orchestration.model_copy(deep=True)
        for group in orchestration.groups:
            group.vehicle_state = {key: value for key, value in group.vehicle_state.items()
                                   if self.start.context.vehicle.state.get(key) != value}
            group.vehicle_state_delta = True
        try:
            return GatewayCheckpoint(start=self.start.model_copy(deep=True), run=CompactRunCheckpoint.capture(state),
                                     semantic_policy_id=self.policy.policy_id, model_policy_id=self.model_policy_id,
                                     orchestration=orchestration, presentation=self._presentation)
        except ValidationError as error:
            if any(item['type'] == 'checkpoint_size_exceeded' for item in error.errors()):
                raise RunProtocolError('Checkpoint continuation state exceeds its resource budget',
                                       code='checkpoint_size_exceeded') from error
            raise

    def checkpoint(self) -> GatewayCheckpoint:
        return self._checkpoint(self._run.checkpoint())

    def _decorate_terminal(self, event):
        self._presentation = completion_presentation(self._checkpoint(self._run.state),
                                                      event.data.get('policy_result'))
        if self._presentation is not None:
            event.data['presentation'] = self._presentation.model_dump()
        return event

    async def _diagnostic(self, record):
        if self.diagnostics is None:
            return
        if self._diagnostic_failed:
            raise _DiagnosticsUnavailable
        self._diagnostic_pending.append(record)
        delta = record.kind == 'run.event' and record.event.type == 'text.delta'
        if delta:
            self._diagnostic_bytes += len(record.event.data['text'].encode())
            if len(self._diagnostic_pending) < 64 and self._diagnostic_bytes < 32768:
                return
        await self._flush_diagnostics()

    async def _flush_diagnostics(self):
        if not self._diagnostic_pending:
            return
        records, self._diagnostic_pending = self._diagnostic_pending, []
        self._diagnostic_bytes = 0

        async def persist():
            # A stalled sink cannot hold cancellation or connection cleanup forever.
            async with asyncio.timeout(10):
                emit_many = getattr(self.diagnostics, 'emit_many', None)
                if callable(emit_many):
                    await emit_many(records)
                else:
                    for record in records:
                        await self.diagnostics.emit(record)

        write = asyncio.create_task(persist())
        cancelled = False
        # Finish an in-flight commit before propagating disconnect cancellation:
        # dropping its ACK would make an automatic resend ambiguous.
        with anyio.CancelScope(shield=True):
            while not write.done():
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
        try:
            write.result()
        except Exception as error:
            self._diagnostic_failed = True
            raise _DiagnosticsUnavailable from error
        if cancelled:
            raise asyncio.CancelledError

    def _diagnostic_failure(self):
        return self._run.fail('diagnostics_unavailable')

    def check_cancelled(self):
        if self._run.cancelled:
            raise asyncio.CancelledError

    async def _child_call(self, coroutine):
        self._child = asyncio.create_task(coroutine)
        try:
            return await self._child
        finally:
            self._child = None

    def history(self):
        return [{'role': item.role, 'content': item.content} for turn in self.start.history[-4:]
                for item in turn.records if item.role in {'user', 'assistant'} and item.content]

    def reserve_child_tool(self):
        if self._run.state.tool_calls + self.orchestration.tool_calls >= self.start.budget.max_tool_calls:
            raise RunProtocolError('tool_call_budget_exhausted', code='tool_call_budget_exhausted')
        self.orchestration.tool_calls += 1

    @property
    def run_state(self) -> RunCheckpoint:
        return self._run.state.model_copy(deep=True)

    def retrieval_rejection(self, request, reserved=()):
        report = retrieval_feedback(self.orchestration.retrieval, request.name)['retrieval_progress']
        limits = RetrievalLimits()
        if (not allow_retrieval(self.orchestration.retrieval, request.name) or request.name in RETRIEVAL_TOOLS and
            (report['calls'] + sum(item.name == request.name for item in reserved) >= limits.max_calls_per_tool or
             report['total_calls'] + sum(item.name in RETRIEVAL_TOOLS for item in reserved) >= limits.max_total_calls)):
            return retrieval_feedback(self.orchestration.retrieval, request.name)
        return None

    def accept_retrieval(self, request, observation):
        if request.name in RETRIEVAL_TOOLS and observation.status != 'unknown':
            self.orchestration.retrieval, _ = record_retrieval(self.orchestration.retrieval,
                invocation_id=request.invocation_id, tool_name=request.name, result=observation.result)

    async def handle_task(self, request, arguments):
        if self.service.task_service is None:
            raise RunProtocolError('The NLP task service is not configured')
        return await self._child_call(self.service.task_service.handle(self.start, request, arguments))

    def completed_vehicle_operations(self):
        operations = {}
        def adopt(identifier, name, arguments, status, result):
            state = result.get('vehicle_state')
            if name not in TOOLS or status != 'completed' or result.get('ok') is not True or result.get('executed') is not True or not isinstance(state, dict):
                return
            # Bind accepted execution facts to their stable NLP invocation. The
            # Gateway may use a separate device command ID; it cannot grant new intent.
            receipt = deepcopy(result)
            receipt.setdefault('name', name)
            receipt.setdefault('arguments', deepcopy(arguments))
            receipt['state'] = deepcopy(state)
            operations[identifier] = {'operation_id': identifier, 'command_id': identifier,
                'name': name, 'arguments': deepcopy(arguments), 'result': receipt}
        for group in self.orchestration.groups:
            for item in group.requests:
                result = group.results.get(item.request.invocation_id)
                if result is not None:
                    adopt(item.request.invocation_id, item.request.name, item.request.arguments, result.status, result.result)
            for plan in group.completed_plans:
                for receipt in plan.receipts:
                    adopt(receipt.invocation_id, receipt.name, receipt.arguments, receipt.status, receipt.result)
        return list(operations.values())

    async def plan_vehicle(self, request, vehicle):
        if self.service.planner is None:
            raise RunProtocolError('The NLP vehicle planner is not configured')
        records = []
        def trace(kind, data, **fields):
            if kind == 'model.started':
                if self._run.state.model_calls + self.orchestration.model_calls >= self.start.budget.max_model_calls:
                    raise RunProtocolError('model_call_budget_exhausted', code='model_call_budget_exhausted')
                self.orchestration.model_calls += 1
            records.append({'kind': kind, 'data': data, **fields})
        remaining = self.start.budget.active_timeout_seconds - self._run.state.active_seconds
        if remaining <= 0:
            raise RunProtocolError('active_time_budget_exhausted', code='active_time_budget_exhausted')
        started = time.monotonic()
        deadline = asyncio.timeout(remaining)
        try:
            async with deadline:
                return await self._child_call(self.service.planner.plan(self.start.user_text, vehicle,
                    request.arguments['skill_ids'], history=self.history(), speaker_seat=self.start.speaker_seat,
                    capabilities=self.start.context.capabilities.features,
                    completed_operations=self.completed_vehicle_operations(), trace=trace))
        except TimeoutError as error:
            if deadline.expired():
                raise RunProtocolError('Planner exhausted the run model time budget', code='active_time_budget_exhausted') from error
            raise
        finally:
            elapsed = time.monotonic() - started
            self.orchestration.active_seconds += elapsed
            self._run.record_model_time(elapsed)
            if self.diagnostics is not None:
                self._diagnostic_pending.extend(DiagnosticRecord(kind='orchestration.event',
                    identity=self.start.identity, data=record) for record in records)
                await self._flush_diagnostics()

    async def _suspend_group(self, requests):
        group = self.orchestration.groups[-1]
        unknown = any(group.receipts.get(item.request.invocation_id) is not None and
                      group.receipts[item.request.invocation_id].status == 'unknown'
                      for item in group.external_pending)
        self._run.wait_for_tools(reconciliation=unknown)
        event = self._run.emit_event('run.suspended', requests=[item.model_dump() for item in requests], status=self._run.state.status)
        output = GatewayOutput(identity=event.identity, sequence=event.sequence, type='run.suspended',
            requests=[DispatchRequest.model_validate(item) for item in requests], checkpoint=self.checkpoint())
        event.data['checkpoint'] = output.checkpoint.model_dump()
        await self._diagnostic(DiagnosticRecord(kind='run.event', identity=event.identity, event=event))
        return output

    async def events(self) -> AsyncIterator[GatewayOutput]:
        if self._active:
            raise RunProtocolError('Concurrent Gateway run advancement is not allowed')
        self._active = True
        observations, self._observations = self._observations, []
        terminal = None
        try:
            if self._run.state.status in {'completed', 'failed', 'cancelled'}:
                raise RunProtocolError('A terminal run cannot be resumed')
            if observations:
                await self._diagnostic(DiagnosticRecord(kind='observations.received', identity=self.start.identity,
                                                        observations=[item.model_copy(deep=True) for item in observations]))
            while terminal is None:
                self.check_cancelled()
                finish = False
                if self.orchestration.groups and not self.orchestration.groups[-1].done:
                    group = ForegroundGroup(self, self.orchestration.groups[-1])
                    outgoing = await group.advance(observations)
                    observations = []
                    if outgoing is not None:
                        yield await self._suspend_group(outgoing)
                        self.check_cancelled()
                        return
                    observations = group.parent_observations()
                    finish = self.service.receipt_fast_path and group.fast_completion()
                if not finish and self._run.state.model_calls + self.orchestration.model_calls >= self.start.budget.max_model_calls:
                    raise RunProtocolError('model_call_budget_exhausted', code='model_call_budget_exhausted')
                async with aclosing(self._run.advance(observations, complete_after_tools=finish)) as stream:
                    async for event in stream:
                        if event.type == 'run.suspended':
                            if not event.data['requests']:
                                raise RunProtocolError('Waiting parent has no orchestration checkpoint')
                            requests = [self._dispatches[item['invocation_id']].model_copy(deep=True) for item in event.data['requests']]
                            vehicle = (self.orchestration.groups[-1].vehicle_state if self.orchestration.groups
                                       else self.start.context.vehicle.state)
                            self.orchestration.groups.append(GroupCheckpoint(requests=requests, vehicle_state=vehicle))
                            continue
                        if event.type in {'run.completed', 'run.cancelled', 'run.failed'}:
                            self._decorate_terminal(event)
                        await self._diagnostic(DiagnosticRecord(kind='run.event', identity=event.identity,
                                                                event=event.model_copy(deep=True)))
                        if event.type == 'text.delta':
                            yield GatewayOutput(identity=event.identity, sequence=event.sequence,
                                                type=event.type, text=event.data['text'])
                        elif event.type in {'run.completed', 'run.cancelled', 'run.failed'}:
                            terminal = event
                observations = []
        except _DiagnosticsUnavailable:
            # A failed diagnostic handoff must not leave undispatched proposals
            # waiting for receipts. Pending calls remain audit-only in a terminal
            # checkpoint; the failing sink is not called recursively.
            terminal = self._diagnostic_failure()
        except asyncio.CancelledError as error:
            externally_cancelled = not self._run.cancelled
            self.cancel()
            terminal = self._run.mark_cancelled(partial=safe_diagnostic(getattr(error, 'trace_partial', {})))
            try:
                await self._diagnostic(DiagnosticRecord(kind='run.event', identity=self.start.identity, event=terminal))
            except _DiagnosticsUnavailable:
                terminal = self._diagnostic_failure()
            if externally_cancelled:
                raise
        except GeneratorExit:
            # A closed consumer has no next anext() on which to deliver a terminal
            # event. Persist partial output now; a handed-off checkpoint stays valid.
            if self._run.state.status not in {'waiting_tools', 'waiting_reconciliation', 'completed', 'cancelled'}:
                self.cancel()
                terminal = self._run.mark_cancelled(reason='consumer_closed')
                try:
                    await self._diagnostic(DiagnosticRecord(kind='run.event', identity=self.start.identity, event=terminal))
                except _DiagnosticsUnavailable:
                    self._diagnostic_failure()
            else:
                await self._flush_diagnostics()
            raise
        except Exception as error:
            if getattr(error, 'code', None) == 'checkpoint_size_exceeded':
                # No handoff is published without a storable checkpoint. The
                # caller retains its preceding checkpoint and must not retry an action.
                raise RunProtocolError(str(error), code='checkpoint_size_exceeded') from error
            if self._run.state.status in {'completed', 'cancelled', 'failed'}:
                raise
            # Wrong/conflicting continuation receipts are retryable, not a new
            # model decision. Child/model failures terminate this inference.
            if observations and isinstance(error, RunProtocolError):
                raise
            if getattr(error, 'uncertain', False) and self.orchestration.groups:
                commit = self.orchestration.groups[-1].task_commit
                if commit is not None:
                    commit.status = 'unknown'
            code = 'task_commit_unknown' if getattr(error, 'uncertain', False) else getattr(error, 'code', None) or 'orchestration_error'
            terminal = self._run.fail(code, error_type=type(error).__name__, policy_result=deepcopy(getattr(error, 'result', {})))
            self._decorate_terminal(terminal)
            try:
                await self._diagnostic(DiagnosticRecord(kind='run.event', identity=self.start.identity, event=terminal))
            except _DiagnosticsUnavailable:
                terminal = self._diagnostic_failure()
        finally:
            self._active = False
        if terminal is not None:
            self._decorate_terminal(terminal)
            yield GatewayOutput(identity=terminal.identity, sequence=terminal.sequence,
                                type=terminal.type, error_code=terminal.data.get('code'),
                                error_details=terminal.data.get('policy_result', {}),
                                checkpoint=self.checkpoint(), presentation=self._presentation)
