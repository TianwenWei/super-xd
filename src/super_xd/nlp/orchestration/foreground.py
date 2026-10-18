"""Receipt-driven foreground groups: NLP children stay inside the service."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from super_xd.nlp.catalog.vehicle import HOT_TOOL_NAMES, TOOLS
from super_xd.nlp.contracts.run import ToolObservation, ToolRequest
from super_xd.nlp.contracts.gateway_v1 import DispatchRequest, NLPDispatch, ToolDispatch, GroupCheckpoint, VehiclePlan, VehicleAction, PlanReceipt, CompletedPlan, TaskCommit
from super_xd.nlp.orchestration.ports import ForegroundHost
from super_xd.nlp.engine.run import RunProtocolError
from super_xd.nlp.policy.vehicle import validate_vehicle_action_keep, validate_vehicle_plan


_MISSING = object()


def _state_value(state, path):
    value = state
    for key in path.split('.'):
        if not isinstance(value, dict) or key not in value:
            return _MISSING
        value = value[key]
    return value


class ForegroundGroup:
    def __init__(self, host: ForegroundHost, state: GroupCheckpoint):
        self.host, self.state = host, state
        if state.cursor > len(state.requests) or state.done != (state.cursor == len(state.requests)):
            raise RunProtocolError('Invalid orchestration cursor')
        if state.external_pending and state.done:
            raise RunProtocolError('A completed group cannot await an external action')
        expected = {item.request.invocation_id for item in state.requests[:state.cursor]}
        if set(state.results) != expected:
            raise RunProtocolError('Resolved parent results do not match the orchestration cursor')
        if state.plan is not None:
            if state.plan_index > len(state.plan.actions) or len(state.plan_receipts) != state.plan_index:
                raise RunProtocolError('Planner cursor differs from its actual receipt ledger')
            if [item.action_id for item in state.plan_receipts] != [item.id for item in state.plan.actions[:state.plan_index]]:
                raise RunProtocolError('Planner receipts do not match the accepted action order')
        elif state.plan_index or state.plan_receipts or state.keep_baseline:
            raise RunProtocolError('Planner state requires an active accepted plan')
        if len({item.request.invocation_id for item in state.requests}) != len(state.requests):
            raise RunProtocolError('Duplicate parent invocation in orchestration group')
        if any(key != value.invocation_id for key, value in {**state.receipts, **state.results}.items()):
            raise RunProtocolError('Receipt ledger key does not match its invocation')
        if state.plan is not None:
            validate_vehicle_plan(state.plan.model_dump(), host.start.user_text, state.vehicle_state,
                                  history=host.history(), completed_operations=state.plan_prior_operations)
        if state.external_pending:
            if state.plan is not None:
                if state.plan_index >= len(state.plan.actions):
                    raise RunProtocolError('A completed plan cannot await an external action')
                original = self._planned_request(state.requests[state.cursor].request, state.plan.actions[state.plan_index])
                if state.external_pending != [original]:
                    raise RunProtocolError('Pending child action differs from the accepted plan')
            else:
                original = state.requests[state.cursor:state.cursor + len(state.external_pending)]
                if state.external_pending != original:
                    raise RunProtocolError('Pending external group differs from the original request group')

    def validate_observations(self, observations):
        """Check the whole receipt batch without adopting facts or applying policy."""
        known = deepcopy(self.state.receipts)
        pending = {item.request.invocation_id: item for item in self.state.external_pending}
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
        return known

    def accept(self, observations):
        self.state.receipts = self.validate_observations(observations)
        known = self.state.receipts
        pending = {item.request.invocation_id: item for item in self.state.external_pending}
        for identifier, outgoing in pending.items():
            value = known.get(identifier)
            if (value is not None and value.status == 'completed' and value.result.get('ok') is True
                    and outgoing.dispatch.operation == 'control' and value.result.get('executed') is not True):
                self.state.receipts[identifier] = ToolObservation(identity=value.identity, invocation_id=identifier,
                    status='unknown', result={'ok': False, 'code': 'execution_not_confirmed'})
            elif (value is not None and value.status == 'completed' and value.result.get('ok') is True
                    and outgoing.dispatch.operation == 'control' and
                    (self.state.plan is not None or outgoing.request.name in HOT_TOOL_NAMES
                     and outgoing.dispatch.prepared_command is None)):
                vehicle = value.result.get('vehicle_state')
                if (not isinstance(vehicle, dict) or not self.state.vehicle_state.keys() <= vehicle.keys()
                        or any(not isinstance(part, dict) for part in vehicle.values())
                        or any(_state_value(vehicle, path) is _MISSING for path in self.state.keep_baseline)):
                    self.state.receipts[identifier] = ToolObservation(identity=value.identity, invocation_id=identifier,
                        status='unknown', result={'ok': False, 'code': 'authoritative_vehicle_state_required'})
        if observations:
            try:
                self.host.policy.validate_observations(self._policy_input(), self._observations())
            except ValueError as error:
                for value in observations:
                    if value.invocation_id in pending and value.status == 'completed':
                        self.state.receipts[value.invocation_id] = ToolObservation(identity=value.identity,
                            invocation_id=value.invocation_id, status='unknown',
                            result={'ok': False, 'code': 'invalid_receipt', 'reason': str(error)})

    def _policy_input(self):
        return {'start': self.host.start.model_dump(), 'checkpoint': self.host.run_state.model_dump(),
                'requests': [item.request.model_dump() for item in self.state.requests]}

    def _observations(self):
        return {key: value.model_dump() for key, value in {**self.state.receipts, **self.state.results}.items()}

    def _finish_parent(self, request, result, *, status='completed'):
        self.state.results[request.invocation_id] = ToolObservation(identity=self.host.start.identity,
            invocation_id=request.invocation_id, status=status, result=result)
        self.state.cursor += 1
        if self.state.plan is not None:
            self.state.completed_plans.append(CompletedPlan(plan=self.state.plan.model_copy(deep=True), receipts=deepcopy(self.state.plan_receipts)))
        self.state.plan = None
        self.state.plan_index = 0
        self.state.plan_receipts = []
        self.state.keep_baseline = {}
        self.state.done = self.state.cursor == len(self.state.requests)

    def _consume_external(self):
        while self.state.external_pending:
            if not self._consume_one():
                return False
        return True

    def _consume_one(self):
        pending = self.state.external_pending[0].request
        receipt = self.state.receipts.get(pending.invocation_id)
        if receipt is None or receipt.status == 'unknown':
            return False
        parent = self.state.requests[self.state.cursor].request
        if self.state.plan is None:
            self.host.accept_retrieval(parent, receipt)
            vehicle = receipt.result.get('vehicle_state')
            if (receipt.status == 'completed' and receipt.result.get('ok') is True and parent.name in HOT_TOOL_NAMES
                    and isinstance(vehicle, dict)):
                self.state.vehicle_state = deepcopy(vehicle)
            self._finish_parent(parent, deepcopy(receipt.result), status=receipt.status)
        else:
            action = self.state.plan.actions[self.state.plan_index]
            record = {'action_id': action.id, 'name': action.name, 'arguments': action.arguments,
                      'invocation_id': pending.invocation_id, 'status': receipt.status, 'result': deepcopy(receipt.result)}
            self.state.plan_receipts.append(PlanReceipt.model_validate(record))
            success = receipt.status == 'completed' and receipt.result.get('ok') is True
            vehicle = receipt.result.get('vehicle_state')
            if vehicle is not None:
                if not isinstance(vehicle, dict) or any(not isinstance(value, dict) for value in vehicle.values()):
                    raise RunProtocolError('Planner receipt vehicle_state must be a complete structured snapshot')
                self.state.vehicle_state = deepcopy(vehicle)
            violated = [path for path, expected in self.state.keep_baseline.items()
                        if success and vehicle is not None and _state_value(vehicle, path) != expected]
            self.state.plan_index += 1
            if violated:
                self._finish_parent(parent, {'ok': False, 'executed': True, 'code': 'keep_constraint_violated',
                    'violated_paths': violated, 'vehicle_state': deepcopy(vehicle),
                    'receipts': [item.model_dump() for item in self.state.plan_receipts],
                    'remaining_actions_not_executed': True})
                self.state.external_pending.pop(0)
                return True
            actions_left = self.state.plan_index < len(self.state.plan.actions)
            requires_state = TOOLS[action.name].state_mapping.get('operation') != 'read'
            if not success or actions_left and requires_state and vehicle is None:
                self._finish_parent(parent, {'ok': False, 'executed': any(item.result.get('executed') is True for item in self.state.plan_receipts),
                    'code': 'plan_action_failed' if not success else 'authoritative_vehicle_state_required',
                    'receipts': [item.model_dump() for item in self.state.plan_receipts], 'remaining_actions_not_executed': True})
        self.state.external_pending.pop(0)
        return True

    def _planned_request(self, parent: ToolRequest, action: VehicleAction):
        operation = 'query' if TOOLS[action.name].state_mapping.get('operation') == 'read' else 'control'
        available = any(item.name == action.name and item.available and item.operation == operation
                        and item.execution_location == 'device' for item in self.host.start.context.capabilities.tools)
        if not available:
            raise RunProtocolError('Planner action is absent from the current device capabilities',
                code='unsupported_capability', details={'ok': False, 'executed': False, 'code': 'unsupported_capability',
                    'reason': 'The device does not advertise the planned action', 'tool_name': action.name, 'retry_model': False})
        identity = hashlib.sha256((parent.invocation_id + ':plan:' + action.id).encode()).hexdigest()
        request = ToolRequest(invocation_id=identity, tool_call_id=action.id, name=action.name,
                              arguments=deepcopy(action.arguments), idempotency_key=identity)
        return DispatchRequest(request=request, dispatch=ToolDispatch(kind='tool_execution', operation=operation,
            execution_location='device', domain=TOOLS[action.name].skill_id, execution_arguments=deepcopy(action.arguments),
            execution_constraints={'keep_baseline': deepcopy(self.state.keep_baseline)}))

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
            self.host.check_cancelled()
            item = self.state.requests[self.state.cursor]
            parent, dispatch = item.request, item.dispatch
            if isinstance(dispatch, NLPDispatch):
                refreshed = self.host.policy.prepare_product_action(self._policy_input(), parent.model_dump(), self._observations())
                dispatch = DispatchRequest(request=parent, dispatch=refreshed).dispatch
                item.dispatch = dispatch
                if dispatch.target != 'vehicle_planner':
                    self.state.task_commit = TaskCommit(invocation_id=parent.invocation_id,
                        idempotency_key=parent.idempotency_key, status='pending',
                        task_id=('task-' + hashlib.sha256(parent.invocation_id.encode()).hexdigest()[:32]
                                 if parent.name == 'delegate_task' else
                                 (dispatch.execution_arguments or parent.arguments).get('task_id')))
                    result = await self.host.handle_task(parent, dispatch.execution_arguments)
                    self.state.task_commit.status = 'confirmed'
                    self._finish_parent(parent, result)
                    continue
                if self.state.plan is None:
                    self.state.plan_prior_operations = self.host.completed_vehicle_operations()
                    plan = await self.host.plan_vehicle(parent, self.state.vehicle_state)
                    empty_failure = (plan.get('status') == 'refused' and plan.get('ok') is False
                        and plan.get('actions') == [] and plan.get('intents') == [] and plan.get('keep_paths') == []
                        and isinstance(plan.get('reason'), str) and bool(plan['reason']))
                    if empty_failure:
                        known = {'planner_model_step_limit', 'invalid_model_message', 'invalid_planner_input'}
                        code = plan.get('code') or (plan['reason'] if plan['reason'] in known else 'vehicle_planning_failed')
                        self._finish_parent(parent, {'ok': False, 'executed': False, 'code': code,
                            'reason': plan['reason'], 'receipts': [], 'plan': plan})
                        continue
                    try:
                        checked = validate_vehicle_plan(plan, self.host.start.user_text, self.state.vehicle_state,
                            history=self.host.history(), completed_operations=self.state.plan_prior_operations)
                        accepted_plan = VehiclePlan.model_validate(checked)
                    except (ValueError, TypeError, KeyError) as error:
                        self._finish_parent(parent, {'ok': False, 'executed': False, 'code': 'vehicle_plan_invalid',
                            'reason': str(error), 'receipts': [], 'remaining_actions_not_executed': True})
                        continue
                    if accepted_plan.status != 'planned':
                        self._finish_parent(parent, {'ok': False, 'executed': False,
                            'needs_clarification': accepted_plan.status == 'clarification',
                            'question': accepted_plan.question, 'reason': accepted_plan.reason, 'plan': accepted_plan.model_dump()})
                        continue
                    self.state.plan = accepted_plan
                    self.state.keep_baseline = {path: deepcopy(_state_value(self.state.vehicle_state, path))
                                                for path in self.state.plan.keep_paths}
                    # Refuse an unavailable later action before releasing the first.
                    for action in self.state.plan.actions:
                        self._planned_request(parent, action)
                    self.state.has_reads |= any(TOOLS[action.name].state_mapping.get('operation') == 'read'
                                                for action in self.state.plan.actions)
                if self.state.plan_index == len(self.state.plan.actions):
                    self._finish_parent(parent, {'ok': True, 'executed': any(item.result.get('executed') is True for item in self.state.plan_receipts),
                        'receipts': [item.model_dump() for item in self.state.plan_receipts], 'vehicle_state': deepcopy(self.state.vehicle_state),
                        'action_ledger': deepcopy(self.state.plan.action_ledger)})
                    continue
                action = self.state.plan.actions[self.state.plan_index]
                validate_vehicle_action_keep(action.name, action.arguments, self.state.vehicle_state,
                                              self.state.plan.keep_paths)
                outgoing = self._planned_request(parent, action)
                self.host.reserve_child_tool()
            else:
                ready = []
                for candidate in self.state.requests[self.state.cursor:]:
                    candidate_request, candidate_dispatch = candidate.request, candidate.dispatch
                    if not isinstance(candidate_dispatch, ToolDispatch):
                        break
                    feedback = self.host.retrieval_rejection(candidate_request, [item.request for item in ready])
                    if feedback is not None:
                        if ready:
                            break
                        self.state.has_reads = True
                        self._finish_parent(candidate_request, feedback, status='rejected')
                        break
                    dependencies = candidate_dispatch.depends_on or []
                    if any(identity not in self.state.results for identity in dependencies):
                        break
                    if any(self.state.results[identity].result.get('ok') is not True for identity in dependencies):
                        if ready:
                            break
                        self._finish_parent(candidate_request, {'ok': False, 'executed': False, 'code': 'dependency_failed'}, status='rejected')
                        break
                    refreshed = self.host.policy.prepare_product_action(self._policy_input(), candidate_request.model_dump(), self._observations())
                    candidate_dispatch = DispatchRequest(request=candidate_request, dispatch=refreshed).dispatch
                    candidate.dispatch = candidate_dispatch
                    self.state.has_reads |= candidate_dispatch.operation == 'query'
                    ready.append(candidate.model_copy(deep=True))
                if not ready:
                    if self.state.done:
                        continue
                    if self.state.requests[self.state.cursor].request.invocation_id != parent.invocation_id:
                        continue
                    raise RunProtocolError('Unresolvable tool dependency')
                self.state.external_pending = deepcopy(ready)
                return ready
            self.state.external_pending = [deepcopy(outgoing)]
            return [outgoing]
        return None

    def parent_observations(self):
        return [self.state.results[item.request.invocation_id] for item in self.state.requests]

    def fast_completion(self):
        if self.state.has_reads:
            return False
        for item in self.state.requests:
            request = item.request
            result = self.state.results[request.invocation_id]
            if result.status != 'completed' or result.result.get('ok') is not True:
                return False
            if isinstance(item.dispatch, NLPDispatch) and item.dispatch.target == 'task_management':
                if request.arguments.get('action') == 'get':
                    return False
        return True
