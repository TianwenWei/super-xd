"""Receipt-driven foreground groups: NLP children stay inside the service."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Any

from pydantic import Field

from backend.catalog import HOT_TOOL_NAMES, TOOLS, execute_tool
from .contracts import Contract, ToolObservation, ToolRequest
from .run import RunProtocolError
from .vehicle_planner import validate_vehicle_action_keep, validate_vehicle_plan


class GroupCheckpoint(Contract):
    requests: list[dict[str, Any]]
    cursor: int = Field(default=0, ge=0)
    external_pending: list[dict[str, Any]] = Field(default_factory=list)
    receipts: dict[str, ToolObservation] = Field(default_factory=dict)
    results: dict[str, ToolObservation] = Field(default_factory=dict)
    plan: dict[str, Any] | None = None
    plan_index: int = Field(default=0, ge=0)
    plan_receipts: list[dict[str, Any]] = Field(default_factory=list)
    completed_plans: list[dict[str, Any]] = Field(default_factory=list)
    task_commit: dict[str, Any] | None = None
    vehicle_state: dict[str, dict[str, Any]] = Field(default_factory=dict)
    has_reads: bool = False
    done: bool = False


class OrchestrationCheckpoint(Contract):
    groups: list[GroupCheckpoint] = Field(default_factory=list)
    model_calls: int = Field(default=0, ge=0)
    active_seconds: float = Field(default=0.0, ge=0)
    tool_calls: int = Field(default=0, ge=0)


def _request(item):
    return ToolRequest.model_validate(item['request'])


class ForegroundGroup:
    def __init__(self, host, state: GroupCheckpoint):
        self.host, self.state = host, state
        if state.cursor > len(state.requests) or state.done != (state.cursor == len(state.requests)):
            raise RunProtocolError('Invalid orchestration cursor')
        if state.external_pending and state.done:
            raise RunProtocolError('A completed group cannot await an external action')

    def accept(self, observations):
        known = deepcopy(self.state.receipts)
        pending = {_request(item).invocation_id: item for item in self.state.external_pending}
        for value in observations:
            item = ToolObservation.model_validate(value.model_dump())
            if item.identity != self.host.start.identity:
                raise RunProtocolError('Action result belongs to another run')
            previous = known.get(item.invocation_id)
            if previous is not None and previous.status != 'unknown':
                if previous != item:
                    raise RunProtocolError('Conflicting receipt for a completed action')
                continue
            if item.invocation_id not in pending:
                raise RunProtocolError('Receipt does not belong to the current external handoff')
            known[item.invocation_id] = item
        self.state.receipts = known
        for identifier, outgoing in pending.items():
            value = known.get(identifier)
            if (value is not None and value.status == 'completed' and value.result.get('ok') is True
                    and outgoing['dispatch']['operation'] == 'control' and value.result.get('executed') is not True):
                self.state.receipts[identifier] = ToolObservation(identity=value.identity, invocation_id=identifier,
                    status='unknown', result={'ok': False, 'code': 'execution_not_confirmed'})
            elif (value is not None and value.status == 'completed' and value.result.get('ok') is True
                    and outgoing['dispatch']['operation'] == 'control' and
                    (self.state.plan is not None or outgoing['request']['name'] in HOT_TOOL_NAMES
                     and outgoing['dispatch'].get('prepared_command') is None)):
                vehicle = value.result.get('vehicle_state')
                if (not isinstance(vehicle, dict) or not self.state.vehicle_state.keys() <= vehicle.keys()
                        or any(not isinstance(part, dict) for part in vehicle.values())):
                    self.state.receipts[identifier] = ToolObservation(identity=value.identity, invocation_id=identifier,
                        status='unknown', result={'ok': False, 'code': 'authoritative_vehicle_state_required'})
        validate = getattr(self.host.policy, 'validate_observations', None)
        if validate is not None and observations:
            try:
                validate(self._policy_input(), self._observations())
            except ValueError as error:
                for value in observations:
                    if value.invocation_id in pending and value.status == 'completed':
                        self.state.receipts[value.invocation_id] = ToolObservation(identity=value.identity,
                            invocation_id=value.invocation_id, status='unknown',
                            result={'ok': False, 'code': 'invalid_receipt', 'reason': str(error)})

    def _policy_input(self):
        return {'start': self.host.start.model_dump(), 'checkpoint': self.host._run.state.model_dump(),
                'requests': [item['request'] for item in self.state.requests]}

    def _observations(self):
        return {key: value.model_dump() for key, value in {**self.state.receipts, **self.state.results}.items()}

    def _finish_parent(self, request, result, *, status='completed'):
        self.state.results[request.invocation_id] = ToolObservation(identity=self.host.start.identity,
            invocation_id=request.invocation_id, status=status, result=result)
        self.state.cursor += 1
        if self.state.plan is not None:
            self.state.completed_plans.append({'plan': deepcopy(self.state.plan), 'receipts': deepcopy(self.state.plan_receipts)})
        self.state.plan = None
        self.state.plan_index = 0
        self.state.plan_receipts = []
        self.state.done = self.state.cursor == len(self.state.requests)

    def _consume_external(self):
        while self.state.external_pending:
            if not self._consume_one():
                return False
        return True

    def _consume_one(self):
        pending = _request(self.state.external_pending[0])
        receipt = self.state.receipts.get(pending.invocation_id)
        if receipt is None or receipt.status == 'unknown':
            return False
        parent = _request(self.state.requests[self.state.cursor])
        if self.state.plan is None:
            vehicle = receipt.result.get('vehicle_state')
            if (receipt.status == 'completed' and receipt.result.get('ok') is True and parent.name in HOT_TOOL_NAMES
                    and isinstance(vehicle, dict)):
                self.state.vehicle_state = deepcopy(vehicle)
            self._finish_parent(parent, deepcopy(receipt.result), status=receipt.status)
        else:
            action = self.state.plan['actions'][self.state.plan_index]
            record = {'action_id': action['id'], 'name': action['name'], 'arguments': action['arguments'],
                      'invocation_id': pending.invocation_id, 'status': receipt.status, 'result': deepcopy(receipt.result)}
            self.state.plan_receipts.append(record)
            success = receipt.status == 'completed' and receipt.result.get('ok') is True
            vehicle = receipt.result.get('vehicle_state')
            if vehicle is not None:
                if not isinstance(vehicle, dict) or any(not isinstance(value, dict) for value in vehicle.values()):
                    raise RunProtocolError('Planner receipt vehicle_state must be a complete structured snapshot')
                self.state.vehicle_state = deepcopy(vehicle)
            self.state.plan_index += 1
            actions_left = self.state.plan_index < len(self.state.plan['actions'])
            requires_state = TOOLS[action['name']].state_mapping.get('operation') != 'read'
            if not success or actions_left and requires_state and vehicle is None:
                self._finish_parent(parent, {'ok': False, 'executed': bool(self.state.plan_receipts),
                    'code': 'plan_action_failed' if not success else 'authoritative_vehicle_state_required',
                    'receipts': deepcopy(self.state.plan_receipts), 'remaining_actions_not_executed': True})
        self.state.external_pending.pop(0)
        return True

    def _planned_request(self, parent, action):
        operation = 'query' if TOOLS[action['name']].state_mapping.get('operation') == 'read' else 'control'
        available = any(item.name == action['name'] and item.available and item.operation == operation
                        and item.execution_location == 'device' for item in self.host.start.context.capabilities.tools)
        if not available:
            raise RunProtocolError('Planner action is absent from the current device capabilities')
        identity = hashlib.sha256((parent.invocation_id + ':plan:' + action['id']).encode()).hexdigest()
        request = ToolRequest(invocation_id=identity, tool_call_id=action['id'], name=action['name'],
                              arguments=deepcopy(action['arguments']), idempotency_key=identity)
        return {'request': request.model_dump(), 'dispatch': {'kind': 'tool_execution', 'operation': operation,
                'execution_location': 'device', 'domain': TOOLS[action['name']].skill_id,
                'execution_arguments': deepcopy(action['arguments']), 'depends_on': [], 'prepared_command': None}}

    async def advance(self, observations=()):
        """Return only newly released actions; None means all parents resolved."""
        before = self.state.model_copy(deep=True)
        try:
            self.accept(observations)
            if not self._consume_external():
                return []
        except Exception:
            for field in type(before).model_fields:
                setattr(self.state, field, getattr(before, field))
            raise
        while not self.state.done:
            self.host._check_cancelled()
            item = self.state.requests[self.state.cursor]
            parent, dispatch = _request(item), item['dispatch']
            if dispatch['kind'] == 'nlp_orchestration':
                prepare = getattr(self.host.policy, 'prepare_product_action', None)
                if prepare is not None:
                    dispatch = prepare(self._policy_input(), parent.model_dump(), self._observations())
                    item['dispatch'] = deepcopy(dispatch)
                if dispatch['target'] != 'vehicle_planner':
                    if self.host.service.task_service is None:
                        raise RunProtocolError('The NLP task service is not configured')
                    self.state.task_commit = {'invocation_id': parent.invocation_id,
                        'idempotency_key': parent.idempotency_key, 'status': 'pending',
                        'task_id': ('task-' + hashlib.sha256(parent.invocation_id.encode()).hexdigest()[:32]
                                    if parent.name == 'delegate_task' else
                                    (dispatch.get('execution_arguments') or parent.arguments).get('task_id'))}
                    result = await self.host._child_call(self.host.service.task_service.handle(
                        self.host.start, parent, dispatch.get('execution_arguments')))
                    self.state.task_commit['status'] = 'confirmed'
                    self._finish_parent(parent, result)
                    continue
                if self.state.plan is None:
                    if self.host.service.planner is None:
                        raise RunProtocolError('The NLP vehicle planner is not configured')
                    plan = await self.host._plan(parent, self.state.vehicle_state)
                    if plan.get('status') != 'planned':
                        self._finish_parent(parent, {'ok': False, 'executed': False,
                            'needs_clarification': plan.get('status') == 'clarification',
                            'question': plan.get('question', ''), 'reason': plan.get('reason', ''), 'plan': plan})
                        continue
                    checked = validate_vehicle_plan(plan, self.host.start.user_text, self.state.vehicle_state,
                        history=self.host._history())
                    self.state.plan = checked
                    # Refuse an unavailable later action before releasing the first.
                    for action in checked['actions']:
                        self._planned_request(parent, action)
                    self.state.has_reads |= any(TOOLS[action['name']].state_mapping.get('operation') == 'read'
                                                for action in checked['actions'])
                if self.state.plan_index == len(self.state.plan['actions']):
                    self._finish_parent(parent, {'ok': True, 'executed': bool(self.state.plan_receipts),
                        'receipts': deepcopy(self.state.plan_receipts), 'vehicle_state': deepcopy(self.state.vehicle_state),
                        'action_ledger': deepcopy(self.state.plan.get('action_ledger', []))})
                    continue
                action = self.state.plan['actions'][self.state.plan_index]
                validate_vehicle_action_keep(action['name'], action['arguments'], self.state.vehicle_state,
                                              self.state.plan['keep_paths'])
                probe = execute_tool(action['name'], action['arguments'], deepcopy(self.state.vehicle_state))
                if probe.get('ok') is not True:
                    self._finish_parent(parent, {'ok': False, 'executed': bool(self.state.plan_receipts),
                        'code': 'action_precheck_rejected', 'reason': probe.get('reason'),
                        'receipts': deepcopy(self.state.plan_receipts), 'remaining_actions_not_executed': True})
                    continue
                outgoing = self._planned_request(parent, action)
                self.host._reserve_child_tool()
            else:
                ready = []
                for candidate in self.state.requests[self.state.cursor:]:
                    candidate_request, candidate_dispatch = _request(candidate), candidate['dispatch']
                    if candidate_dispatch['kind'] != 'tool_execution':
                        break
                    dependencies = candidate_dispatch.get('depends_on') or []
                    if any(identity not in self.state.results for identity in dependencies):
                        break
                    if any(self.state.results[identity].result.get('ok') is not True for identity in dependencies):
                        if ready:
                            break
                        self._finish_parent(candidate_request, {'ok': False, 'executed': False, 'code': 'dependency_failed'}, status='rejected')
                        break
                    prepare = getattr(self.host.policy, 'prepare_product_action', None)
                    if prepare is not None:
                        candidate_dispatch = prepare(self._policy_input(), candidate_request.model_dump(), self._observations())
                        candidate['dispatch'] = deepcopy(candidate_dispatch)
                    self.state.has_reads |= candidate_dispatch['operation'] == 'query'
                    ready.append({'request': candidate_request.model_dump(), 'dispatch': deepcopy(candidate_dispatch)})
                if not ready:
                    if self.state.done:
                        continue
                    if _request(self.state.requests[self.state.cursor]).invocation_id != parent.invocation_id:
                        continue
                    raise RunProtocolError('Unresolvable tool dependency')
                self.state.external_pending = deepcopy(ready)
                return ready
            self.state.external_pending = [deepcopy(outgoing)]
            return [outgoing]
        return None

    def parent_observations(self):
        return [self.state.results[_request(item).invocation_id] for item in self.state.requests]

    def fast_completion(self):
        if self.state.has_reads:
            return False
        for item in self.state.requests:
            request = _request(item)
            result = self.state.results[request.invocation_id]
            if result.status != 'completed' or result.result.get('ok') is not True:
                return False
            if item['dispatch']['kind'] == 'nlp_orchestration' and item['dispatch']['target'] == 'task_management':
                if request.arguments.get('action') == 'get':
                    return False
        return True
