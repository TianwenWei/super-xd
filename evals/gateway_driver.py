"""Behavior fixtures exercising the production NLPService without SessionActor."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import json
import time
from uuid import uuid4

from backend.catalog import TOOLS
from backend.cockpit import CockpitServices, LocalMediaCatalog
from backend.cockpit.controller import scoped_place
from backend.cockpit.device import DeviceRequest
from backend.demo.cockpit_device import CockpitDevice
from backend.demo.local_gateway import LocalGateway, history_records
from backend.demo.resources import LocalResources, freeze_local_model, local_task_budget, local_model_policy_id
from backend.harness.background import TaskService
from backend.harness.contracts import RunIdentity, ToolObservation
from backend.harness.gateway import NLPService
from backend.harness.gateway_contracts import GatewayOutput, HistoryTurn, StartRequest
from backend.harness.product_policy import ProductTalkerPolicy
from backend.harness.model_execution import extract_usage
from backend.harness.task_runner import TaskRunner
from backend.harness.vehicle_planner import VehicleControlPlanner
from backend.manual_search import ManualSearchClient
from backend.models import utcnow, sanitized_error
from backend.services.contracts import ProviderError
from backend.harness.diagnostics import safe_diagnostic
from backend.services.factory import create_maps, create_search


def model_secrets(model):
    """Configured adapters retain secrets even when a profile uses custom key_env."""
    providers = getattr(model, 'providers', {})
    values = [model, *providers.values()] if isinstance(providers, dict) else [model]
    return tuple(secret for provider in values for secret in getattr(provider, 'secrets', ())
                 if isinstance(secret, str) and secret)


@dataclass
class TurnProgress:
    started: float
    vehicle_before: dict
    task_count: int
    request: StartRequest | None = None
    last: GatewayOutput | None = None
    driver: LocalGateway | None = None
    output: str = ''
    first_text_ms: float | None = None
    first_model_text_ms: float | None = None
    presentation_ms: float | None = None
    presentation_source: str | None = None
    first_by_call: dict = field(default_factory=dict)
    finished: float | None = None
    failure: dict | None = None


class MemoryTaskLedger:
    def __init__(self, root=None, conversation_id=None):
        self.root = root or self
        if root is None:
            self.tasks, self.operations, self.events = {}, {}, []
        self.conversation_id = conversation_id

    def for_scope(self, conversation_id, **lease):
        return type(self)(self.root, conversation_id)

    async def read(self, task_id):
        task = self.root.tasks.get(task_id)
        return deepcopy(task) if task and task['session_id'] == self.conversation_id else None

    async def refresh(self, task_id):
        return await self.read(task_id)

    async def create_task(self, task_id, goal, context, idempotency_key):
        signature = json.dumps([self.conversation_id, task_id, goal, context], sort_keys=True)
        if idempotency_key in self.root.operations:
            previous, result = self.root.operations[idempotency_key]
            if previous != signature:
                raise ValueError('Idempotency key conflict')
            return deepcopy(result)
        now = utcnow()
        result = {'id': task_id, 'session_id': self.conversation_id, 'goal': goal, 'context': deepcopy(context),
                  'status': 'queued', 'revision': 1, 'summary': '', 'constraints': [], 'sources': [],
                  'result': None, 'question': None, 'steps': 0, 'checkpoint': {}, 'transcript': [],
                  'running_seconds': 0, 'running_since': None, 'created_at': now, 'updated_at': now,
                  'deadline_at': (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()}
        self.root.tasks[task_id] = deepcopy(result)
        self.root.operations[idempotency_key] = signature, deepcopy(result)
        return result

    async def mutate_task(self, task_id, revision, action, text, idempotency_key):
        signature = json.dumps([self.conversation_id, task_id, revision, action, text])
        if idempotency_key in self.root.operations:
            previous, result = self.root.operations[idempotency_key]
            if previous != signature:
                raise ValueError('Idempotency key conflict')
            return deepcopy(result)
        task = await self.read(task_id)
        if task is None or task['revision'] != revision:
            raise ValueError('Task revision conflict')
        task.update(revision=revision + 1, status='cancelling' if action == 'cancel' else 'queued',
                    result=None, question=None)
        if action != 'cancel':
            task['constraints'].append(text)
        self.root.tasks[task_id] = deepcopy(task)
        self.root.operations[idempotency_key] = signature, deepcopy(task)
        return task

    async def save(self, task_id, revision, **fields):
        task = await self.read(task_id)
        if task is None or task['revision'] != revision or task['status'] in {'completed', 'failed', 'cancelled'}:
            return False
        self.root.tasks[task_id].update(deepcopy(fields))
        return True

    async def emit(self, session_id, kind, data, **fields):
        self.root.events.append({'session_id': session_id, 'kind': kind, 'data': deepcopy(data), **fields})

    async def expire(self, task_id, max_age_seconds):
        return None

    async def notify_progress(self, task_id):
        return True

    async def usage(self, task_id, session_id):
        return 0.0, 0


class EvaluationGateway:
    def __init__(self, case, model, *, planner=None, maps=None):
        from backend.demo.device import VehicleSimulator
        from backend.demo.vehicle_simulator import initial_vehicle_state
        model = freeze_local_model(model)
        self.case, self.model = case, model
        self.vehicle = VehicleSimulator(initial_vehicle_state(), execution_ms=0, network_rtt_ms=0)
        self.device = CockpitDevice()
        self.maps, self.web, self.manual = maps or create_maps(), create_search(), ManualSearchClient()
        self.owns_maps = maps is None
        self.cockpit = (CockpitServices(LocalMediaCatalog(case['cockpit'].get('items', [])))
                        if 'cockpit' in case else None)
        self.resources = LocalResources(maps=self.maps, web=self.web, manual=self.manual, cockpit=self.cockpit)
        self.capabilities, bindings = self.resources.declarations()
        for capability in self.capabilities:
            if capability.name in case.get('lookup_fixtures', {}):
                capability.available = True
        self.ledger = MemoryTaskLedger()
        self.diagnostics, self.history, self.places, self.media = [], [], {}, {}
        self.traces, self.trace_sequence = [], 0
        self.fixture_indices = Counter()
        self.revision = 0
        self.progress = None
        budget = local_task_budget()
        self.policy_id = local_model_policy_id(model, budget)
        tasks = TaskService(self.ledger, lambda store: TaskRunner(store, model, self.web, trace_sink=store,
                            close_clients=False, **asdict(budget)),
                            model_policy_id=self.policy_id)
        self.service = NLPService(model, model_policy_id=self.policy_id,
            semantic_policy=ProductTalkerPolicy(bindings,
                enabled_nlp_targets=['vehicle_planner', 'thinker_task', 'task_management']),
            diagnostics=self, planner=planner or VehicleControlPlanner(model), task_service=tasks)

    async def emit(self, record):
        self.diagnostics.append(record.model_dump())
        if record.kind == 'run.event':
            event = record.event
            data = deepcopy(event.data)
            if event.type == 'model.completed':
                data['meta'] = data.pop('metadata', {})
            kind = 'model.error' if event.type == 'run.failed' and data.get('error_type') else event.type
            self.record(kind, data, record.identity.turn_id)
        elif record.kind == 'orchestration.event':
            self.record(record.data['kind'], record.data['data'], record.identity.turn_id)
        else:
            self.record(record.kind, record.model_dump(), record.identity.turn_id)

    async def emit_many(self, records):
        for record in records:
            await self.emit(record)

    def record(self, kind, data, turn_id):
        self.trace_sequence += 1
        self.traces.append({'trace_id': self.trace_sequence, 'turn_id': turn_id, 'kind': kind,
                            'data': deepcopy(data), 'created_at': utcnow()})

    async def execute(self, item, start):
        request, dispatch = item.request, item.dispatch
        arguments = deepcopy(dispatch.execution_arguments if dispatch.execution_arguments is not None else request.arguments)
        self.calls.append({'name': request.name, 'arguments': deepcopy(arguments)})
        if request.name in TOOLS and dispatch.prepared_command is None:
            wire = DeviceRequest(request.invocation_id, start.conversation_id, start.identity.turn_id,
                'vehicle', request.name, arguments, dispatch.operation, 'eval-device', 12000).to_wire()
            wire['name'] = request.name
            self.vehicle.register(wire, keep=deepcopy(dispatch.execution_constraints.get('keep_baseline', {})))
            self.record('vehicle.dispatched', wire, start.identity.turn_id)
            result = await self.vehicle.execute(wire['command_id'])
            result['vehicle_state'] = deepcopy(result['state'])
            self.record('vehicle.receipt', {'command_id': wire['command_id'], 'result': result}, start.identity.turn_id)
            self.commands.append({'name': request.name, 'arguments': deepcopy(arguments),
                                  'source': 'talker' if request.name in {row['name'] for row in self.talker_calls} else 'vehicle_plan'})
        elif dispatch.operation == 'query' and request.name != 'control_media':
            self.lookups.append(request.name)
            fixtures = self.case.get('lookup_fixtures', {}).get(request.name)
            if fixtures is not None:
                if not fixtures:
                    raise ValueError('A lookup fixture requires at least one result')
                index = self.fixture_indices[request.name]
                self.fixture_indices[request.name] += 1
                result = deepcopy(fixtures[min(index, len(fixtures) - 1)])
            elif request.name == 'manage_navigation' and arguments.get('action') == 'get_status':
                state = deepcopy(self.device.state['navigation'])
                result = {'ok': True, 'state': state, 'state_version': state['version']}
            else:
                try:
                    result = await self.resources.query(item)
                except Exception as error:
                    result = {'ok': False, 'executed': False,
                        'code': 'timeout' if isinstance(error, TimeoutError) else getattr(error, 'code', 'lookup_error'),
                        'reason': sanitized_error(error)}
                    if isinstance(error, ProviderError):
                        result.update(provider_error=error.public(), diagnostics=error.details)
                    if getattr(error, 'trace_partial', None) is not None:
                        result['partial'] = error.trace_partial
                    result = safe_diagnostic(result)
            self.lookup_results.append({'tool': request.name, 'arguments': deepcopy(arguments),
                                        'result': deepcopy(result)})
            if request.name == 'search_places' and result.get('ok'):
                for place in result.get('places', []):
                    place = scoped_place(place, result.get('provider'))
                    self.places[place['place_ref']] = {**place, 'source_turn_id': start.identity.turn_id,
                                                      'presented_to_user': True}
            if request.name == 'search_media' and result.get('ok'):
                self.media = {row['content_id']: {**row, 'provider': row.get('provider') or result.get('provider'),
                    'source_turn_id': start.identity.turn_id, 'presented_to_user': True} for row in result['items']}
        else:
            command = await self.resources.resolve_command(item)
            if command is None:
                state = deepcopy(self.device.state[dispatch.domain])
                result = {'ok': True, 'state': state, 'state_version': state['version']}
            else:
                wire = DeviceRequest(request.invocation_id, start.conversation_id, start.identity.turn_id,
                    command['domain'], command['action'], command['arguments'], 'control', 'eval-device', 12000).to_wire()
                wire['expected_state_version'] = command['expected_state_version']
                receipt = self.device.execute(wire)
                result = {**receipt, 'executed': receipt['ok'], 'domain': command['domain']}
                self.cockpit_commands.append({'domain': command['domain'], 'action': command['action'],
                                              'arguments': deepcopy(command['arguments'])})
        self.record('tool.completed', {'name': request.name, 'arguments': arguments, 'result': result}, start.identity.turn_id)
        if (dispatch.operation == 'control' and result.get('ok') is True
                and result.get('executed') is True and result.get('outcome_unknown') is not True):
            self.last_execution_receipt_ms = (time.perf_counter() - self.turn_started) * 1000
        return ToolObservation(identity=start.identity, invocation_id=request.invocation_id,
                               status='unknown' if result.get('outcome_unknown') is True else 'completed', result=result)

    @property
    def talker_calls(self):
        calls = []
        for row in self.diagnostics:
            event = row.get('event') or {}
            if event.get('type') == 'model.completed':
                proposed = event['data']['message'].get('tool_calls', [])
                if not isinstance(proposed, list):
                    continue
                for call in proposed:
                    if not isinstance(call, dict) or not isinstance(call.get('function'), dict):
                        continue
                    function = call['function']
                    arguments = function.get('arguments')
                    try:
                        arguments = json.loads(arguments)
                    except (TypeError, ValueError):
                        pass  # Preserve malformed proposals as data in failed-turn reports.
                    calls.append({'name': function.get('name'), 'arguments': arguments})
        return calls

    def start_request(self, text, persona, speaker_seat):
        self.revision += 1
        now, turn_id = utcnow(), uuid4().hex[:12]
        state = deepcopy(self.vehicle.state)
        meta = state['_meta']
        context = {'revision': self.revision, 'observed_at': now,
            'vehicle': {'revision': self.vehicle.version, 'observed_at': now, 'state': state},
            'capabilities': {'revision': 1, 'observed_at': now,
                'tools': [item.model_dump() for item in self.capabilities],
                'features': {name: True for name in TOOLS},
                'cockpit': self.cockpit.info() if self.cockpit else {'configured': False}},
            'environment': {'timezone': meta['timezone'], 'location':
                {'name': meta['location'], 'country': meta.get('location_country', ''), 'source': 'user_setting'}
                if meta.get('location') else None},
            'cockpit': deepcopy(self.device.state) if self.cockpit else {},
            'tasks': [{key: task[key] for key in ('id', 'revision', 'status', 'goal', 'summary', 'context', 'result', 'question')}
                      for task in self.ledger.tasks.values()],
            'places': [dict(place_ref=key, name=value['name'], provider=value['provider'],
                source_turn_id=value['source_turn_id'], presented_to_user=True, details=value) for key, value in self.places.items()],
            'media': [{key: value[key] for key in ('content_id', 'kind', 'title', 'provider', 'source_turn_id', 'presented_to_user')}
                      for value in self.media.values()]}
        return StartRequest(identity=RunIdentity(run_id=uuid4().hex, request_id=uuid4().hex,
            turn_id=turn_id, context_revision=self.revision), conversation_id='eval-' + self.case['id'],
            received_at=now, user_text=text, speaker_seat=speaker_seat, persona_id=persona,
            history=self.history[-4:], context=context)

    async def turn(self, spec, persona, speaker_seat):
        self.diagnostics, self.calls, self.commands, self.cockpit_commands = [], [], [], []
        self.traces = []
        self.lookups, self.lookup_results = [], []
        progress = self.progress = TurnProgress(time.perf_counter(), deepcopy(self.vehicle.state), len(self.ledger.tasks))
        self.turn_started, self.last_execution_receipt_ms = progress.started, None
        start = self.start_request(spec['input'], persona, speaker_seat)
        progress.request = start
        self.record('turn.started', {'input': spec['input']}, start.identity.turn_id)
        driver = progress.driver = LocalGateway(self.service, start, self.execute)
        async for event in driver.events():
            progress.last = event
            if event.type == 'text.delta' and event.text:
                elapsed = (time.perf_counter() - progress.started) * 1000
                progress.first_text_ms = progress.first_text_ms if progress.first_text_ms is not None else elapsed
                if progress.first_model_text_ms is None:
                    progress.first_model_text_ms = elapsed
                index = sum((record.get('event') or {}).get('type') == 'model.started' for record in self.diagnostics)
                progress.first_by_call.setdefault(index, elapsed)
                progress.output += event.text
            if event.presentation is not None:
                progress.presentation_ms = (time.perf_counter() - progress.started) * 1000
                progress.presentation_source = event.presentation.source
                progress.first_text_ms = progress.first_text_ms if progress.first_text_ms is not None else progress.presentation_ms
                progress.output += event.presentation.speech
        if progress.last is None or progress.last.checkpoint is None:
            raise RuntimeError('NLP did not return a committed checkpoint')
        checkpoint = progress.last.checkpoint
        records = history_records(self.service.turn_records(checkpoint))
        playback = spec.get('playback', {})
        status = playback.get('status', 'completed')
        pending = {call['id'] for record in records if record['role'] == 'assistant' for call in record.get('tool_calls', [])}
        pending -= {record['tool_call_id'] for record in records if record['role'] == 'tool'}
        self.history.append(HistoryTurn(turn_id=start.identity.turn_id, speaker_seat=speaker_seat,
            records=records, pending_tool_group=bool(pending), interrupted_playback=status != 'completed', heard_text=playback.get('heard_text', progress.output),
            **({'playback_status': status} if 'playback_status' in HistoryTurn.model_fields else {})))
        progress.finished = time.perf_counter()
        error = progress.last.error_code or ('outcome_unknown' if checkpoint.run.status == 'waiting_reconciliation' else None)
        self.record('turn.completed', {'output': progress.output, 'error': error}, start.identity.turn_id)
        return self.observe_turn()

    def failed_turn(self, error):
        """Observe an aborted advance without discarding or retrying its effects."""
        progress = self.progress
        if progress is None:
            raise RuntimeError('No evaluation turn has started')
        if progress.failure is None:
            code = getattr(error, 'code', None) or ('turn_timeout' if isinstance(error, TimeoutError) else 'run_failed')
            failure = {'code': code, 'error_type': type(error).__name__, 'message': str(error)}
            if isinstance(error, ProviderError):
                failure.update(provider_error=error.public(), diagnostics=error.details)
            elif getattr(error, 'result', None) is not None:
                failure['details'] = error.result
            if getattr(error, 'trace_partial', None) is not None:
                failure['partial'] = error.trace_partial
            progress.failure = safe_diagnostic(failure, model_secrets(self.model))
            progress.finished = time.perf_counter()
            self.record('turn.failed', {**progress.failure, 'output': progress.output},
                        progress.request.identity.turn_id if progress.request else None)
        return self.observe_turn()

    def observe_turn(self):
        """One public statistics snapshot for completed, suspended and failed turns."""
        progress = self.progress
        if progress is None:
            raise RuntimeError('No evaluation turn has started')
        roles = Counter()
        usage = Counter()
        prefaces = []
        for row in self.diagnostics:
            event = row.get('event') or {}
            if event.get('type') == 'model.started':
                roles['talker'] += 1
            elif row['kind'] == 'orchestration.event' and row.get('data', {}).get('kind') == 'model.started':
                roles['planner'] += 1
            if event.get('type') == 'model.completed':
                if event['data']['message'].get('tool_calls') and event['data']['message'].get('content'):
                    prefaces.append(event['data']['message']['content'])
                metadata = event['data'].get('metadata', {})
            elif row['kind'] == 'orchestration.event' and row.get('data', {}).get('kind') == 'model.completed':
                metadata = row['data']['data'].get('meta', {})
            else:
                continue
            tokens = extract_usage(metadata)
            for key, value in [('prompt_tokens', tokens.input_tokens), ('completion_tokens', tokens.output_tokens),
                               ('total_tokens', tokens.total_tokens)]:
                if value is not None:
                    usage[key] += value
            values = metadata.get('usage')
            details = values.get('prompt_tokens_details') if isinstance(values, dict) else None
            cached = details.get('cached_tokens') if isinstance(details, dict) else None
            if type(cached) is int and cached >= 0:
                usage['cached_tokens'] += cached
        last = progress.last
        checkpoint = last.checkpoint if last else None
        if checkpoint is None and progress.driver is not None and progress.driver.commit is not None:
            checkpoint = progress.driver.commit.checkpoint
        native = checkpoint.run.messages if checkpoint else []
        route = ('task' if any(row['name'] in {'delegate_task', 'manage_task'} for row in self.talker_calls)
                 else 'vehicle' if self.commands else 'lookup' if self.lookups else 'chat')
        outcome_unknown = checkpoint is not None and checkpoint.run.status == 'waiting_reconciliation'
        error = (last.error_code if last else None) or ('outcome_unknown' if outcome_unknown else None)
        failure = progress.failure
        if failure is not None:
            error = failure['code']
            commit = progress.driver.commit if progress.driver else None
            outcome_unknown |= error == 'checkpoint_size_exceeded' or bool(commit and any(
                getattr(item.dispatch, 'operation', None) == 'control' for item in commit.requests))
        # Error templates and incomplete model text do not establish a useful
        # result. A failed turn can still retain an actually executed control.
        if error:
            first_useful = self.last_execution_receipt_ms
        elif progress.presentation_source == 'verified_receipts':
            first_useful = progress.presentation_ms
        elif native and native[-1]['role'] == 'assistant' and not native[-1].get('tool_calls'):
            first_useful = progress.first_by_call.get(roles['talker'])
        else:
            first_useful = self.last_execution_receipt_ms
        result = {'turn_id': progress.request.identity.turn_id if progress.request else None,
            'commands': self.commands, 'cockpit_commands': self.cockpit_commands,
            'cockpit_device_mode': 'explicit_eval_fixture' if self.cockpit else None, 'cockpit': deepcopy(self.device.state),
            'talker_calls': self.talker_calls, 'lookups': self.lookups, 'lookup_results': self.lookup_results,
            'vehicle': deepcopy(self.vehicle.state), 'vehicle_before': progress.vehicle_before,
            'tasks_created': len(self.ledger.tasks) - progress.task_count,
            'route': route, 'error': error, 'outcome_unknown': outcome_unknown, 'text': progress.output, 'preface': ''.join(prefaces) or None,
            'model_calls': sum(roles.values()), 'model_calls_by_role': dict(roles),
            'talker_model_calls': roles['talker'], 'total_ms': ((progress.finished or time.perf_counter()) - progress.started) * 1000,
            'first_effective_ms': progress.first_text_ms, 'first_useful_ms': first_useful, 'usage': dict(usage),
            'first_model_text_ms': progress.first_model_text_ms, 'presentation_source': progress.presentation_source,
            'first_speech_ms': None, 'trace': deepcopy(self.traces),
            'model_errors': [row for row in self.traces if row['kind'] == 'model.error']}
        if failure is not None:
            result.update(error_type=failure['error_type'], diagnostic=failure['message'], exception=failure)
        return safe_diagnostic(result, model_secrets(self.model))

    async def close(self):
        await self.vehicle.close()
        if self.owns_maps:
            await self.maps.aclose()
        await self.web.aclose()
        await self.manual.aclose()
